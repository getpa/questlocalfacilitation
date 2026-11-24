package questhub;

import java.io.BufferedOutputStream;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.PrintStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.util.List;

/**
 * Streams one Android sensor (by default the Quest headset IMU) to stdout as text lines.
 *
 * Runs on the headset with app_process as the adb shell user, the way scrcpy runs its server:
 *   CLASSPATH=/data/local/tmp/questhub-imu-XXXXXXXX.dex app_process / questhub.ImuStream [options]
 *
 * Options:
 *   --list               print the sensors and exit
 *   --sensor TYPE|NAME   sensor to read (default oculus.sensor.hmd_imu.2, the headset IMU)
 *   --rate HZ            output rate; samples are averaged into bins of 1/HZ (0 = every sample, raw)
 *   --seconds N          stop after N seconds
 *
 * Output:
 *   # questhub-imu 2
 *   # sensor name=... type=... string_type=... vendor=... max_rate_hz=...
 *   # columns t_us,n,temp_c,ax,ay,az,gx,gy,gz      (headset IMU; other sensors: t,n,v0,v1,...)
 *   <rows>
 *   # sync t=<sensor timestamp of the newest sample> mono_ns=<CLOCK_MONOTONIC> wall_ms=<device clock>
 *
 * The headset IMU delivers about 800 samples/s whatever rate is requested, with timestamps in microseconds
 * of its own clock (not CLOCK_MONOTONIC: about 9.5 s apart on a Quest 3). Values: [temperature C,
 * accel x y z (m/s^2), gyro x y z (rad/s), zeros].
 * Rows carry the mean timestamp and the mean of every value of the n samples in the bin.
 * Output is flushed 5 times a second. About once a second a sync line is printed and flushed at once,
 * so the reader can map sensor time to its own clock.
 *
 * Everything Android is reached through reflection and java.lang.reflect.Proxy, so this file compiles with
 * a plain JDK (no Android SDK) and is converted to DEX with d8 (scripts/build_imu_helper.sh).
 */
public final class ImuStream {
    static final String HEADSET_IMU = "oculus.sensor.hmd_imu.2";
    static final int VERSION = 2;
    static final int IMU_VALUES = 7;   // temperature, accel xyz, gyro xyz

    private static PrintStream out;

    public static void main(String[] args) throws Exception {
        String wanted = HEADSET_IMU;
        int rateHz = 100;
        boolean list = false;
        double seconds = 0;
        for (int i = 0; i < args.length; i++) {
            String a = args[i];
            if (a.equals("--list")) list = true;
            else if (a.equals("--sensor")) wanted = args[++i];
            else if (a.equals("--rate")) rateHz = Integer.parseInt(args[++i]);
            else if (a.equals("--seconds")) seconds = Double.parseDouble(args[++i]);
            else { System.err.println("unknown option: " + a); System.exit(2); }
        }
        out = new PrintStream(new BufferedOutputStream(new FileOutputStream(FileDescriptor.out), 1 << 16), false);

        Class<?> looperClass = Class.forName("android.os.Looper");
        looperClass.getMethod("prepareMainLooper").invoke(null);
        Object looper = looperClass.getMethod("getMainLooper").invoke(null);
        Class<?> ssm = Class.forName("android.hardware.SystemSensorManager");
        Class<?> contextClass = Class.forName("android.content.Context");
        Object manager = ssm.getConstructor(contextClass, looperClass).newInstance(systemContext(), looper);

        Class<?> sensorClass = Class.forName("android.hardware.Sensor");
        List<?> sensors = (List<?>) manager.getClass().getMethod("getSensorList", int.class).invoke(manager, -1);
        Object chosen = null;
        out.println("# questhub-imu " + VERSION);
        for (Object s : sensors) {
            if (list) out.println("# sensor " + describe(sensorClass, s));
            String type = String.valueOf(sensorClass.getMethod("getType").invoke(s));
            String stringType = String.valueOf(sensorClass.getMethod("getStringType").invoke(s));
            if (chosen == null && (wanted.equals(type) || wanted.equals(stringType))) chosen = s;
        }
        if (list) { out.flush(); return; }
        if (chosen == null) {
            out.println("# error sensor not found: " + wanted);
            out.flush();
            System.exit(3);
        }
        final boolean imu = HEADSET_IMU.equals(sensorClass.getMethod("getStringType").invoke(chosen));
        out.println("# sensor " + describe(sensorClass, chosen));
        out.println(imu ? "# columns t_us,n,temp_c,ax,ay,az,gx,gy,gz" : "# columns t,n,values...");
        out.flush();

        // Bin length in sensor time units: microseconds for the headset IMU, nanoseconds for Android sensors.
        final long bin = rateHz <= 0 ? 0 : (imu ? 1_000_000L : 1_000_000_000L) / rateHz;
        final long stopAt = seconds > 0 ? System.nanoTime() + (long) (seconds * 1e9) : Long.MAX_VALUE;

        Class<?> listenerClass = Class.forName("android.hardware.SensorEventListener");
        Class<?> eventClass = Class.forName("android.hardware.SensorEvent");
        final Field valuesField = eventClass.getField("values");
        final Field timestampField = eventClass.getField("timestamp");
        final StringBuilder line = new StringBuilder(256);
        final double[] sum = new double[32];
        final long[] state = new long[6];   // 0 bin start, 1 n, 2 sum of (t - bin start), 3 last flush ns, 4 last t,
                                            // 5 last sync ns
        state[3] = System.nanoTime();
        state[5] = state[3] - 2_000_000_000L;   // a sync line right at the first sample
        final int[] width = {0};

        InvocationHandler handler = new InvocationHandler() {
            @Override
            public Object invoke(Object proxy, Method method, Object[] a) throws Throwable {
                String name = method.getName();
                if (name.equals("onSensorChanged")) {
                    onSample(timestampField.getLong(a[0]), (float[]) valuesField.get(a[0]));
                    return null;
                }
                if (name.equals("hashCode")) return System.identityHashCode(proxy);
                if (name.equals("equals")) return proxy == a[0];
                if (name.equals("toString")) return "questhub.ImuListener";
                return null;   // onAccuracyChanged
            }

            private void onSample(long t, float[] v) {
                int w = imu ? Math.min(IMU_VALUES, v.length) : Math.min(sum.length, v.length);
                if (state[1] > 0 && (bin == 0 || t - state[0] >= bin || w != width[0])) emit();
                if (state[1] == 0) {
                    state[0] = bin == 0 ? t : t - Math.floorMod(t, bin);
                    width[0] = w;
                }
                state[1]++;
                state[2] += t - state[0];
                for (int i = 0; i < w; i++) sum[i] += v[i];
                state[4] = t;
                long now = System.nanoTime();
                boolean sync = now - state[5] > 1_000_000_000L;
                if (sync) {
                    out.println("# sync t=" + t + " mono_ns=" + now + " wall_ms=" + System.currentTimeMillis());
                    state[5] = now;
                }
                if (sync || now - state[3] > 200_000_000L) {   // flush 5x per second, and right after a sync
                    out.flush();
                    state[3] = now;
                    if (out.checkError()) System.exit(0);   // the reader went away
                }
                if (now >= stopAt) {
                    if (state[1] > 0) emit();
                    out.flush();
                    System.exit(0);
                }
            }

            private void emit() {
                long n = state[1];
                line.setLength(0);
                line.append(state[0] + state[2] / n).append(',').append(n);
                for (int i = 0; i < width[0]; i++) {
                    line.append(',').append((float) (sum[i] / n));
                    sum[i] = 0;
                }
                out.println(line);
                state[1] = 0;
                state[2] = 0;
            }
        };
        Object listener = Proxy.newProxyInstance(ImuStream.class.getClassLoader(), new Class<?>[]{listenerClass}, handler);
        int periodUs = rateHz <= 0 ? 0 : Math.max(1, 1_000_000 / rateHz);
        boolean ok = (Boolean) manager.getClass().getMethod("registerListener", listenerClass, sensorClass, int.class)
                .invoke(manager, listener, chosen, periodUs);
        if (!ok) {
            out.println("# error registerListener failed");
            out.flush();
            System.exit(4);
        }
        looperClass.getMethod("loop").invoke(null);
    }

    private static String describe(Class<?> sensorClass, Object s) throws Exception {
        String name = String.valueOf(sensorClass.getMethod("getName").invoke(s));
        if (name.startsWith("{")) {   // the headset IMU's name is a JSON blob with its calibration
            int i = name.indexOf("\"aname\":\"");
            name = i >= 0 ? name.substring(i + 9, name.indexOf('"', i + 9)) : "json";
        }
        int minDelayUs = (Integer) sensorClass.getMethod("getMinDelay").invoke(s);
        return "name=" + name.replace(' ', '_')
                + " type=" + sensorClass.getMethod("getType").invoke(s)
                + " string_type=" + sensorClass.getMethod("getStringType").invoke(s)
                + " vendor=" + String.valueOf(sensorClass.getMethod("getVendor").invoke(s)).replace(' ', '_')
                + " max_rate_hz=" + (minDelayUs > 0 ? 1_000_000 / minDelayUs : 0);
    }

    /** The system context, built the way scrcpy does it for code started with app_process. */
    private static Object systemContext() throws Exception {
        Class<?> at = Class.forName("android.app.ActivityThread");
        Constructor<?> ctor = at.getDeclaredConstructor();
        ctor.setAccessible(true);
        Object thread = ctor.newInstance();
        Field current = at.getDeclaredField("sCurrentActivityThread");
        current.setAccessible(true);
        current.set(null, thread);
        try {
            Field systemThread = at.getDeclaredField("mSystemThread");
            systemThread.setAccessible(true);
            systemThread.setBoolean(thread, true);
        } catch (NoSuchFieldException ignored) {
        }
        return at.getMethod("getSystemContext").invoke(thread);
    }
}
