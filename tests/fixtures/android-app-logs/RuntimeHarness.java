import android.app.Activity;
import android.app.Application;
import android.content.Intent;
import android.view.View;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Set;
import io.reproloop.autotrace.ReproAppLogs;

public final class RuntimeHarness {
    private static final String RUN_ID = "11111111-1111-4111-8111-111111111111";
    private static final String NEXT_RUN_ID = "22222222-2222-4222-8222-222222222222";
    private static final String DIGEST = "0000000000000000000000000000000000000000000000000000000000000000";

    public static void main(String[] args) throws Exception {
        Path files = Files.createTempDirectory("repro-app-logs");
        Application application = new Application(files.toFile(), "demo.app");
        Activity first = activity(application, files, true);
        first.setChangingConfigurations(true);
        ReproAppLogs.start(first, DIGEST, "fixture", 1, Set.of("save"));
        application.created(first);
        application.started(first);
        application.resumed(first);
        first.findViewById(1).getViewTreeObserver().dispatch();
        first.removeView(2);
        first.findViewById(1).getViewTreeObserver().dispatch();
        first.addView(2, new AlternateView(first), "screen_root");
        first.findViewById(1).getViewTreeObserver().dispatch();

        long returned = ReproAppLogs.tapBegan(first, "save");
        long nested = ReproAppLogs.tapBegan(first, "save");
        check(returned < 0 && nested < 0 && returned != nested, "globally disjoint nested observation tokens");
        ReproAppLogs.tapReturned(nested);
        ReproAppLogs.tapReturned(returned);
        long threw = ReproAppLogs.tapBegan(first, "save");
        ReproAppLogs.tapThrew(threw);
        application.saved(first);
        application.paused(first);
        application.stopped(first);
        application.destroyed(first);

        Activity second = activity(application, files, true);
        ReproAppLogs.start(second, DIGEST, "fixture", 1, Set.of("save"));
        application.created(second);
        application.started(second);
        application.resumed(second);
        second.findViewById(1).getViewTreeObserver().dispatch();
        application.paused(second);
        application.stopped(second);

        Path marker = files.resolve("repro/app-log-session.json");
        check(Files.isRegularFile(marker), "identity marker exists");
        String markerText = Files.readString(marker);
        check(markerText.contains("\"runId\":\"" + RUN_ID + "\""), "marker run identity");
        String sessionId = markerText.substring(markerText.indexOf("\"sessionId\":\"") + 13);
        sessionId = sessionId.substring(0, sessionId.indexOf('"'));
        Path journal = files.resolve("repro/app-logs").resolve(sessionId).resolve("app-log.json");
        String log = waitFor(journal);
        for (String name : new String[]{"attached", "created", "started", "resumed", "paused", "stopped",
                "save_state", "destroyed", "foreground", "background", "appeared", "disappeared", "began", "returned", "threw"}) {
            check(log.contains("\"name\":\"" + name + "\""), "event " + name);
        }
        check(count(log, "\"name\":\"foreground\"") == 1 &&
                count(log, "\"name\":\"background\"") == 1, "recreation has no background pair");
        check(log.contains("\"target\":\"save\""), "tap target");
        check(log.contains("\"target\":\"main\""), "screen target");
        check(count(log, "\"name\":\"disappeared\"") >= 3, "removed and replaced screens disappear");
        check(!log.contains("RuntimeHarness"), "raw class names excluded");
        check(!log.contains("button-label"), "raw labels excluded");

        Path journalParent = journal.getParent();
        Files.deleteIfExists(journal);
        Files.delete(journalParent);
        Files.writeString(journalParent, "blocked");
        long lost = ReproAppLogs.tapBegan(second, "save");
        Thread.sleep(80);
        Files.delete(journalParent);
        Files.createDirectories(journalParent);
        ReproAppLogs.tapReturned(lost);
        log = waitForContains(journal, "\"lostEvents\":true");
        check(log.contains("\"name\":\"returned\""), "loss retry preserves later snapshot");

        String oldJournal = log;
        Activity next = activity(application, files, NEXT_RUN_ID);
        ReproAppLogs.start(next, DIGEST, "fixture", 1, Set.of("save"));
        application.created(first);
        application.created(next);
        waitForText(marker, "\"runId\":\"" + NEXT_RUN_ID + "\"");
        check(Files.readString(journal).equals(oldJournal), "old session remains immutable across runs");

        Activity invalid = activity(application, files, "invalid");
        ReproAppLogs.start(invalid, DIGEST, "fixture", 1, Set.of("save"));
        check(Files.readString(marker).contains("\"runId\":\"" + NEXT_RUN_ID + "\""), "invalid run does not replace marker");

        Path badFiles = files.resolve("not-a-directory");
        Files.writeString(badFiles, "file");
        Application badApplication = new Application(badFiles.toFile(), "demo.app");
        Activity bad = activity(badApplication, badFiles, "33333333-3333-4333-8333-333333333333");
        ReproAppLogs.start(bad, DIGEST, "fixture", 1, Set.of("save"));
        Thread.sleep(100);
        check(Files.readString(marker).contains("\"runId\":\"" + NEXT_RUN_ID + "\""), "failed initialization leaves marker unchanged");
        observationMode();
    }

    private static void observationMode() throws Exception {
        Path files = Files.createTempDirectory("repro-views-observations");
        Application application = new Application(files.toFile(), "demo.app");
        Activity view = activity(application, files, RUN_ID);
        view.setIntent(new Intent().putExtra("repro_log_run_id", RUN_ID)
            .putExtra("repro_mode", "observe").putExtra("repro_observation_profile", DIGEST));
        ReproAppLogs.startObservation(view, DIGEST, Set.of("save"));
        application.started(view);
        application.resumed(view);
        long token = ReproAppLogs.tapBegan(view, "save");
        check(token < 0, "observation mode needs no fixture");
        ReproAppLogs.tapThrew(token);
        ReproAppLogs.tapReturned(token);
        Path marker = files.resolve("repro/app-log-session.json");
        waitForText(marker, "\"runId\":\"" + RUN_ID + "\"");
        String first = Files.readString(marker);
        String sessionId = first.substring(first.indexOf("\"sessionId\":\"") + 13).split("\"")[0];
        String log = waitForContains(files.resolve("repro/app-logs/" + sessionId + "/app-log.json"), "\"threw\"");
        check(!log.contains("\"returned\""), "throw has exactly one terminal observation");
        check(!log.contains("fixture") && !log.contains("button-label"), "no fixture or raw text captured");
        Activity wrong = activity(application, files, NEXT_RUN_ID);
        wrong.setIntent(new Intent().putExtra("repro_log_run_id", NEXT_RUN_ID)
            .putExtra("repro_mode", "observe").putExtra("repro_observation_profile", "f".repeat(64)));
        ReproAppLogs.startObservation(wrong, DIGEST, Set.of("save"));
        check(ReproAppLogs.tapBegan(wrong, "save") == 0L, "foreign observation run is rejected");
        wrong.setIntent(new Intent().putExtra("repro_log_run_id", NEXT_RUN_ID)
            .putExtra("repro_mode", "record").putExtra("repro_observation_profile", DIGEST));
        ReproAppLogs.startObservation(wrong, DIGEST, Set.of("save"));
        Thread.sleep(80);
        check(Files.readString(marker).equals(first), "wrong profile and mode preserve original session");
        view.setIntent(new Intent().putExtra("repro_log_run_id", NEXT_RUN_ID)
            .putExtra("repro_mode", "observe").putExtra("repro_observation_profile", DIGEST));
        ReproAppLogs.startObservation(view, DIGEST, Set.of("save"));
        waitForText(marker, "\"runId\":\"" + NEXT_RUN_ID + "\"");
        check(!Files.readString(marker).equals(first), "new observation launch rotates journal identity");
    }

    private static Activity activity(Application application, Path files, boolean valid) {
        return activity(application, files, valid ? RUN_ID : "invalid");
    }

    private static Activity activity(Application application, Path files, String runId) {
        Activity activity = new Activity(files.toFile(), "demo.app");
        activity.setApplication(application);
        activity.getApplicationInfo().flags = android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE;
        activity.setIntent(new Intent().putExtra("repro_mode", "record")
                .putExtra("fixture_id", "fixture").putExtra("fixture_version", 1)
                .putExtra("repro_log_run_id", runId));
        View content = new View(activity);
        View screen = new View(activity);
        activity.addView(1, content, "content");
        activity.addView(2, screen, "screen_root");
        return activity;
    }

    private static String waitFor(Path journal) throws Exception {
        for (int attempt = 0; attempt < 100; attempt++) {
            if (Files.isRegularFile(journal)) {
                String value = Files.readString(journal);
                if (value.contains("\"threw\"") && value.contains("\"name\":\"background\"")) return value;
            }
            Thread.sleep(20);
        }
        throw new AssertionError("journal did not persist");
    }

    private static void waitForText(Path file, String text) throws Exception {
        for (int attempt = 0; attempt < 100; attempt++) {
            if (Files.isRegularFile(file) && Files.readString(file).contains(text)) return;
            Thread.sleep(20);
        }
        throw new AssertionError("file did not contain expected identity");
    }

    private static String waitForContains(Path file, String text) throws Exception {
        for (int attempt = 0; attempt < 100; attempt++) {
            if (Files.isRegularFile(file)) {
                String value = Files.readString(file);
                if (value.contains(text)) return value;
            }
            Thread.sleep(20);
        }
        throw new AssertionError("journal did not contain expected state");
    }

    private static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }

    private static int count(String text, String value) {
        int count = 0;
        for (int offset = 0; (offset = text.indexOf(value, offset)) >= 0; offset += value.length()) count++;
        return count;
    }

    private static final class AlternateView extends View {
        AlternateView(android.content.Context context) { super(context); }
    }
}
