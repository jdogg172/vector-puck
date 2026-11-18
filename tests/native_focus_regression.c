/* Build with the prepared upstream checkout:
 * cc -std=gnu11 -Wall -Wextra -Werror -Isrc/hassmic native_focus_regression.c src/hassmic/wyoming.c -o focus-test
 * ./focus-test ./build/hassmic-host
 * Requires bwrap and ffmpeg with flite. All simulated device files live in private tmpfs.
 */
#define _GNU_SOURCE
#include "wyoming.h"
#include <arpa/inet.h>
#include <assert.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define PRIVACY "/sys/devices/soc/10010000.keypad/amz_privacy"
#define AUDIO "{\"rate\":24000,\"width\":2,\"channels\":1}"
#define CHUNK 4800
#define CHUNKS 105

static struct wy_reader peer;
static struct wy_event event;
static unsigned played, cancels, stops, pipelines, chunks;
static unsigned char reply[CHUNKS * CHUNK];

static double now(void)
{
    struct timespec t;
    assert(!clock_gettime(CLOCK_MONOTONIC, &t));
    return t.tv_sec + t.tv_nsec / 1e9;
}

static void file_text(const char *file, const char *text)
{
    FILE *f = fopen(file, "w");
    assert(f && fputs(text, f) >= 0 && !fclose(f));
}

static void command(char *const argv[])
{
    pid_t pid = fork();
    assert(pid >= 0);
    if (!pid) { execvp(argv[0], argv); perror(argv[0]); _exit(127); }
    int status;
    assert(waitpid(pid, &status, 0) == pid && WIFEXITED(status) && !WEXITSTATUS(status));
}

static void fixture(void)
{
    assert(!mkdir("state", 0700) && !mkdir("etc", 0700) && !mkdir("etc/led-resources", 0700));
    assert(!mkfifo("keypad", 0600) && open("keypad", O_RDWR | O_CLOEXEC) >= 0);
    file_text("state/config", "even_volume=off\n");
    file_text("state/adopted", "ha\n");
    file_text(PRIVACY "/state", "0\n");
    file_text("beam", "-1\n");
    const char *config = "{\"numberOfLayers\":6,\"Layers\":{\"0\":[\"zzz_rainbow\"],\"1\":[\"mics-off_on\"],"
        "\"2\":[\"call_incoming\"],\"3\":[\"ready-timer\"],\"4\":[\"active-end\",\"active-start\",\"active-thinking\","
        "\"active-talking\",\"ntt-listening\"],\"5\":[\"volume_step-05\"]}}";
    file_text("etc/layer_config_common.json.pre-hassmic", config);
    const char *base = "1000:00F,00F,00F,00F,00F,00F,00F,00F,00F,00F,0FF,0FF\n";
    file_text("etc/led-resources/active-start.animation", base);
    file_text("etc/led-resources/active-end.animation", "loop\n100:000,000,000,000,000,000,000,000,000,000,000,000\n");
    file_text("etc/led-resources/active-thinking.animation", base);
    file_text("etc/led-resources/active-talking.animation", base);
    file_text("etc/led-resources/mics-off_on.animation", "loop\n1000:F00,F00,F00,F00,F00,F00,F00,F00,F00,F00,F00,F00\n");
    file_text("etc/led-resources/ntt-listening.animation", "loop\n1000:089,089,089,089,089,089,089,089,089,089,089,089\n");
    assert(!setenv("HASSMIC_LED_ROOT", "/tmp/etc", 1) && !setenv("HASSMIC_STATE", "/tmp/state", 1));
    assert(!setenv("HASSMIC_FAKE_BEAM", "/tmp/beam", 1) && !setenv("HASSMIC_FAKE_LED", "/tmp/led.log", 1));
    assert(!setenv("HASSMIC_PLAY", "/tmp/output.raw", 1) && !setenv("HASSMIC_CAP", "/tmp/input.raw", 1));
    command((char *[]){"./daemon", "-G", "etc/led-resources", NULL});
    assert(!rename("etc/led-resources/digest", "etc/hassmic-leds.digest"));

    /* Read the installed hold frames. A name alone does not prove that the
     * corresponding asset lights exactly its one full-cyan pixel on black. */
    for (int pixel = 0; pixel < 12; pixel++) {
        char name[80], line[160];
        snprintf(name, sizeof name, "etc/led-resources/hassmic_led_%d_12f0.animation", pixel);
        FILE *asset = fopen(name, "r");
        assert(asset && fgets(line, sizeof line, asset) && !strcmp(line, "loop\n"));
        assert(fgets(line, sizeof line, asset) && !strncmp(line, "1000:", 5) && !fclose(asset));
        char *save = NULL, *rgb = strtok_r(line + 5, ",\r\n", &save);
        for (int i = 0; i < 12; i++) {
            assert(rgb && !strcmp(rgb, i == pixel ? "00FFFF" : "000000"));
            rgb = strtok_r(NULL, ",\r\n", &save);
        }
        assert(!rgb);
    }
    command((char *[]){"ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
        "flite=text=Hey Vector. Keep speaking while I move to the other side of the room.",
        "-ar", "24000", "-ac", "1", "-f", "s16le", "input.raw", NULL});
    FILE *f = fopen("input.raw", "rb");
    assert(f);
    size_t size = fread(reply, 1, sizeof reply, f);
    assert(size > CHUNK && !fclose(f) && memcmp(reply, reply + size / 3, CHUNK));
    for (size_t i = size; i < sizeof reply; ++i) reply[i] = reply[i % size];
}

static pid_t start_daemon(void)
{
    pid_t pid = fork();
    assert(pid >= 0);
    if (!pid) {
        assert(!prctl(PR_SET_PDEATHSIG, SIGKILL));
        execl("./daemon", "hassmic", "-P", "wyoming", "-w", "remote", "-p", "16700",
              "-b", "/tmp/keypad", "-B", "-z", "0", "-a", "0", "-i", "0", "-W", "0", "-o", "0", (char *)NULL);
        _exit(127);
    }
    struct sockaddr_in addr = {.sin_family = AF_INET, .sin_port = htons(16700), .sin_addr.s_addr = htonl(INADDR_LOOPBACK)};
    for (int i = 0; i < 200; i++) {
        int fd = socket(AF_INET, SOCK_STREAM, 0);
        assert(fd >= 0);
        if (!connect(fd, (struct sockaddr *)&addr, sizeof addr)) {
            struct timeval timeout = {4, 0};
            assert(!setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof timeout));
            assert(!wy_reader_init(&peer, fd));
            return pid;
        }
        close(fd);
        assert(waitpid(pid, NULL, WNOHANG) == 0);
        usleep(25000);
    }
    assert(0 && "daemon failed to listen");
    return -1;
}

static void send_type(const char *kind, const char *json)
{
    assert(wy_write(peer.fd, kind, json, NULL, 0) == 0);
}

static void expect(const char *kind)
{
    double deadline = now() + 5;
    do {
        assert(now() < deadline && wy_read(&peer, &event) == 1);
        played += !strcmp(event.type, "played");
        cancels += !strcmp(event.type, "vector-cancel");
        stops += !strcmp(event.type, "audio-stop");
        pipelines += !strcmp(event.type, "run-pipeline");
        chunks += !strcmp(event.type, "audio-chunk");
    } while (strcmp(event.type, kind));
}

static void barrier(void) { send_type("ping", NULL); expect("pong"); }

/* Reconstruct the current active set from actual ledctrl commands. Check both
 * pixels and every unset, so a stale third pixel cannot pass a direction test. */
static unsigned lights(const char *name, int *on)
{
    FILE *f = fopen("led.log", "r");
    char op[4], pat[80], line[128];
    unsigned pixels = 0;
    *on = 0;
    if (!f) return 0;
    while (fgets(line, sizeof line, f)) {
        if (sscanf(line, "%3s %79s", op, pat) != 2) continue;
        int enabled = !strcmp(op, "-s");
        if (!strcmp(name, pat)) *on = enabled;
        int pixel, end = 0;
        if (sscanf(pat, "hassmic_led_%d_12f0%n", &pixel, &end) == 1 && end && !pat[end]) {
            assert(pixel >= 0 && pixel < 12);
            if (enabled) pixels |= 1u << pixel; else pixels &= ~(1u << pixel);
        }
    }
    fclose(f);
    return pixels;
}

static void frame(const char *name, int enabled, unsigned pixels, double timeout)
{
    double deadline = now() + timeout;
    for (;;) {
        int on;
        unsigned seen = lights(name, &on);
        if (on == enabled && seen == pixels) return;
        if (now() >= deadline) {
            fprintf(stderr, "expected %s=%d mask=%03x; got %d mask=%03x\n", name, enabled, pixels, on, seen);
            exit(1);
        }
        usleep(10000);
    }
}

/* Independently reconstruct the top-view centre of light from ledctrl's active
 * assets. X points to the seated observer's right, Y towards 12 o'clock. */
static void geometry(int expected_x, int expected_y)
{
    static const int x[12] = {0,500,866,1000,866,500,0,-500,-866,-1000,-866,-500};
    static const int y[12] = {1000,866,500,0,-500,-866,-1000,-866,-500,0,500,866};
    int sx = 0, sy = 0;
    for (int i = 0; i < 12; i++) {
        for (int level = 0; level <= 1; level++) {
            char name[48];
            int on;
            snprintf(name, sizeof name, "hassmic_led_%d_12f%d", i, level);
            lights(name, &on);
            if (on) {
                int weight = level == 0 ? 255 : 102;
                sx += x[i] * weight;
                sy += y[i] * weight;
            }
        }
    }
    assert(sx * expected_y == sy * expected_x && sx * expected_x + sy * expected_y > 0);
}

static void calibration(void)
{
    const char *neutral = "hassmic_solid_12f2";
    file_text("beam", "6");
    frame(neutral, 0, 0x030, .6);        /* equal pixels 4 and 5, observer sees 4:30 */
    geometry(1, -1);
    /* Local setting wins on a run with no override. Invalid input falls back
     * to the device file, and later rooms never inherit another run's override. */
    file_text("state/vector-ring-offset-deg", "45\n");
    send_type("run-satellite", NULL); barrier();
    frame(neutral, 0, 1u << 6, .5);
    geometry(0, -1);
    file_text("beam", "9");               /* same correction at a second position */
    frame(neutral, 0, 1u << 9, .5);
    geometry(-1, 0);
    file_text("beam", "6");
    send_type("run-satellite", "{\"vector_ring_offset_deg\":-45}"); barrier();
    frame(neutral, 0, 1u << 3, .5);
    geometry(1, 0);
    send_type("run-satellite", "{\"vector_ring_offset_deg\":200}"); barrier();
    frame(neutral, 0, 1u << 6, .5);
    send_type("run-satellite", "{\"vector_ring_offset_deg\":1.5}"); barrier();
    frame(neutral, 0, 1u << 6, .5);
    file_text("beam", "0");
    frame(neutral, 0, 1, .5);             /* wraps through LEDs 11, 0, 1 */
    geometry(0, 1);
    assert(!unlink("state/vector-ring-offset-deg"));
    send_type("run-satellite", NULL); barrier();
    frame(neutral, 0, 0xc00, .5);
    file_text("beam", "45");               /* degrees are not valid LED indices */
    frame(neutral, 1, 0, .5);
    double worst = 0;
    for (int i = 0; i < 10; i++) {
        double start = now();
        file_text("beam", i % 2 ? "3" : "6");
        frame(neutral, 0, i % 2 ? 0x006 : 0x030, .8);
        double delay = now() - start;
        if (delay > worst) worst = delay;
    }
    printf("PASS: clockwise geometry, per-room +45/-45/0 and invalid input; max of 10 beam updates %.1f ms\n", worst * 1000);
    assert(worst < .125);
}

static void send_pcm(void)
{
    for (unsigned i = 0; i < CHUNKS; i++)
        assert(!wy_write(peer.fd, "audio-chunk", AUDIO, reply + i * CHUNK, CHUNK));
}

static off_t output_size(void)
{
    struct stat st;
    return stat("output.raw", &st) ? 0 : st.st_size;
}

static void check_playback(void)
{
    FILE *f = fopen("output.raw", "rb");
    unsigned char block[CHUNK];
    assert(f && output_size() == (off_t)sizeof reply);
    for (unsigned i = 0; i < CHUNKS; i++) {
        assert(fread(block, 1, CHUNK, f) == CHUNK);
        assert(!memcmp(block, reply + i * CHUNK, CHUNK));
    }
    fclose(f);
}

static void question_handoff(void)
{
    unsigned old_played = played, old_cancels = cancels, old_runs;
    off_t initial = output_size();
    send_type("detection", NULL);
    send_type("transcript", "{\"text\":\"first question\"}");
    send_type("vector-listen", NULL);
    send_type("audio-start", AUDIO); expect("run-pipeline");
    send_pcm();
    double deadline = now() + 2;
    while (output_size() < initial + CHUNK && now() < deadline) usleep(10000);
    assert(output_size() >= initial + CHUNK);
    send_type("vector-focus", NULL); barrier();
    old_runs = pipelines;
    assert(played == old_played);
    double start = now();
    send_type("vector-question", NULL);
    send_type("vector-question", NULL);    /* duplicate after promotion is inert */
    send_type("audio-stop", NULL);         /* exactly one end for the old output */
    expect("played");
    expect("audio-chunk"); barrier();
    off_t cut = output_size();
    assert(now() - start < 1.5 && cut < initial + (off_t)sizeof reply / 2);
    assert(pipelines == old_runs && cancels == old_cancels && played == old_played + 1);
    frame("active-talking", 0, 0x180, .6);
    send_type("transcript", "{\"text\":\"new question\"}");
    frame("active-thinking", 1, 0, .6);   /* old Q_STOP kept the new question listening */
    send_type("vector-listen", NULL);
    send_type("audio-start", AUDIO); expect("run-pipeline");
    assert(strstr(event.json, "\"vector_barge_in\":true"));
    const unsigned char *next = reply + CHUNK * 11;
    assert(!wy_write(peer.fd, "audio-chunk", AUDIO, next, CHUNK));
    send_type("audio-stop", NULL); expect("played"); expect("run-pipeline");
    FILE *out = fopen("output.raw", "rb");
    unsigned char actual[CHUNK];
    assert(out && !fseeko(out, cut, SEEK_SET) && fread(actual, 1, CHUNK, out) == CHUNK);
    assert(!memcmp(actual, next, CHUNK) && fgetc(out) == EOF && !fclose(out));
    assert(pipelines == old_runs + 2 && cancels == old_cancels && played == old_played + 2);
    puts("PASS: recognized question flushed queued speech, preserved live mic across old played, and delivered the new answer byte-for-byte");
}

static void test(void)
{
    const char *neutral = "hassmic_solid_12f2", *talk = "active-talking";
    send_type("describe", NULL); expect("info");
    assert(strstr(event.json, "\"vector_focus\":true") && strstr(event.json, "\"vector_question\":true"));
    send_type("run-satellite", NULL); expect("run-pipeline");
    send_type("vector-indicator", "{\"kind\":\"message\",\"duration_ms\":60000}");
    send_type("vector-focus", NULL); send_type("vector-question", NULL); barrier();
    frame("hassmic_pulse_w0", 1, 0, .5);
    send_type("detection", "{\"name\":\"hey_vector\"}");
    frame(neutral, 1, 0, .6);
    calibration();
    file_text("beam", "0");
    frame(neutral, 0, 0xc00, .6);    /* 10,11 */
    file_text("beam", "3");
    frame(neutral, 0, 0x006, .6);    /* 1,2 */
    send_type("vector-alarm", "{\"action\":\"start\",\"id\":\"0123456789abcdef0123456789abcdef\",\"name\":\"test\"}");
    frame("hassmic_hold_3", 1, 0, .6);    /* timer below the stock listening layer */
    send_type("vector-alarm", "{\"action\":\"stop\",\"id\":\"0123456789abcdef0123456789abcdef\"}");
    frame("hassmic_hold_3", 0, 0x006, .6);
    send_type("vector-focus", NULL);
    send_type("transcript", "{\"text\":\"\"}");
    frame("active-thinking", 1, 0, .6);
    send_type("audio-start", AUDIO);
    send_type("vector-question", NULL);   /* no authorized overlap yet */
    send_type("vector-focus", NULL);
    send_type("detection", NULL);
    barrier();
    frame(talk, 1, 0, .6);           /* speaking without an authorized overlap */
    send_type("vector-listen", NULL);
    expect("run-pipeline");
    assert(strstr(event.json, "\"vector_barge_in\":true"));
    send_pcm();
    double deadline = now() + 2;
    while (output_size() < CHUNK && now() < deadline) usleep(10000);
    assert(output_size() >= CHUNK);
    off_t before = output_size();
    file_text("beam", "-1");
    double focus = now();
    send_type("vector-focus", NULL);
    frame(neutral, 1, 0, .6);
    file_text("beam", "6");
    frame(neutral, 0, 0x030, .6);    /* 4,5 */
    file_text("beam", "-1");
    frame(neutral, 1, 0, .6);
    file_text("beam", "9");
    frame(neutral, 0, 0x180, .6);    /* 7,8 */
    send_type("detection", "{\"name\":\"hey_vector\"}");  /* legacy detection during speech */
    send_type("vector-focus", "{\"duration_ms\":999999}");
    barrier();
    unsigned mic_before = chunks;
    usleep(350000);
    barrier();
    assert(chunks > mic_before && output_size() > before);
    frame(talk, 0, 0x180, .5);
    frame(talk, 1, 0, 8.0);
    double elapsed = now() - focus;
    assert(elapsed >= 7.95 && elapsed < 8.65);
    barrier();
    assert(!played && !cancels && !stops && pipelines == 2);
    send_type("audio-stop", NULL);
    expect("played"); expect("run-pipeline");
    assert(played == 1 && !cancels && pipelines == 3);
    check_playback();
    frame("hassmic_pulse_w0", 1, 0, 1);
    printf("PASS: beams 0/3/6/9 and unknown; %.2f s focus; %zu speech bytes identical; one natural played\n",
           elapsed, sizeof reply);

    question_handoff();
    unsigned played_before_stops = played;
    /* End an active lease by reply completion, explicit stop and hardware mute. */
    for (int mode = 0; mode < 3; mode++) {
        send_type("detection", NULL);
        send_type("transcript", "{\"text\":\"\"}");
        send_type("vector-listen", NULL);
        send_type("audio-start", AUDIO); expect("run-pipeline");
        send_type("vector-focus", NULL);
        frame(talk, 0, 0x180, .6);
        if (mode == 0) {
            send_type("audio-stop", NULL);
        } else {
            send_pcm();
            if (mode == 1) send_type("vector-stop", NULL);
            else file_text(PRIVACY "/state", "1\n");
            expect("vector-cancel");
        }
        expect("played");
        if (mode != 2) {
            expect("run-pipeline");
            frame("hassmic_pulse_w0", 1, 0, 1);
        }
    }
    frame("mics-off_on", 1, 0, 1);
    send_type("vector-focus", NULL); send_type("vector-question", NULL); send_type("detection", NULL); barrier();
    unsigned mic = chunks, runs = pipelines;
    usleep(350000); barrier();
    assert(chunks == mic && pipelines == runs && cancels == 2 && played == played_before_stops + 3);
    frame("hassmic_pulse_w0", 0, 0, .5);
    puts("PASS: natural end, explicit stop and mute clear focus; muted peer receives no mic audio or pipeline");
}

int main(int argc, char **argv)
{
    if (argc == 2) {
        char self[PATH_MAX], binary[PATH_MAX];
        assert(realpath("/proc/self/exe", self) && realpath(argv[1], binary));
        execlp("bwrap", "bwrap", "--unshare-all", "--die-with-parent", "--new-session",
               "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/sys", "--dir", PRIVACY,
               "--tmpfs", "/tmp", "--ro-bind", self, "/tmp/focus-test", "--ro-bind", binary, "/tmp/daemon",
               "--clearenv", "--setenv", "PATH", "/usr/bin:/bin", "--chdir", "/tmp", "/tmp/focus-test", "--inside", "fixture", (char *)NULL);
        perror("bwrap"); return 1;
    }
    if (argc != 3 || strcmp(argv[1], "--inside") || strcmp(argv[2], "fixture") || access("./daemon", X_OK)) {
        fprintf(stderr, "usage: %s ./build/hassmic-host\n", argv[0]); return 2;
    }
    alarm(50);
    fixture();
    for (int assets = 0; assets < 3; assets++) {
        if (assets) {
            file_text(PRIVACY "/state", "0\n");
            file_text("beam", "-1\n");
            file_text("led.log", "");
            assert(!unlink("etc/hassmic-leds.digest"));
            if (assets == 1) {
                assert(!setenv("HASSMIC_RING_LEDS", "0", 1));
                command((char *[]){"./daemon", "-G", "etc/led-resources", NULL});
                assert(!rename("etc/led-resources/digest", "etc/hassmic-leds.digest"));
            }
        }
        pid_t pid = start_daemon();
        if (!assets) test();
        else {
            send_type("run-satellite", NULL); expect("run-pipeline");
            send_type("detection", NULL);
            frame(assets == 1 ? "hassmic_solid_12f2" : "ntt-listening", 1, 0, .6);
            file_text("beam", "3");
            frame(assets == 1 ? "hassmic_hold_3" : "active-start", 1, 0, .6);
            file_text("beam", "7");
            frame(assets == 1 ? "hassmic_hold_7" : "active-start", 1, 0, .6);
            send_type("run-satellite", "{\"vector_ring_offset_deg\":45}"); barrier();
            frame(assets == 1 ? "hassmic_hold_9" : "ntt-listening", 1, 0, .6);
            puts(assets == 1 ? "PASS: rotated stock hold without per-LED layers" : "PASS: stock-only neutral and native beam animation");
        }
        close(peer.fd);
        wy_reader_free(&peer);
        kill(pid, SIGTERM);
        assert(waitpid(pid, NULL, 0) == pid);
    }
    return 0;
}
