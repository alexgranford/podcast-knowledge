"""Several users: who is asking, following shows, queueing within a limit, and who gets which message."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

import notify
import podqueue
import podshows
import users

APPLE = "https://podcasts.apple.com/gb/podcast/id1999999901?i=1000000000001"
PEOPLE = {"jo": {"name": "Jo", "role": "admin", "identities": ["telegram:111", "whatsapp:440000000001"], "deliver": "telegram:111"},
          "robin": {"name": "Robin", "role": "member", "identities": ["whatsapp:440000000002"], "deliver": "whatsapp:440000000002"},
          "kit": {"name": "Kit", "role": "member", "identities": ["whatsapp:440000000003"], "deliver": "whatsapp:440000000003",
                   "limit_per_day": 1}}


@pytest.fixture
def people(tmp_path, monkeypatch):
    users.STATE.mkdir(parents=True, exist_ok=True)
    users.path().write_text(json.dumps(PEOPLE))
    podqueue.STATE.mkdir(parents=True, exist_ok=True)
    registry = {"1999999901": {"title": "Harbour Talk", "folder": "Harbour Talk", "country": "gb", "followers": ["jo"],
                               "hosts": [], "fixes": {}, "advertisers": {}}}
    podshows.STATE.mkdir(parents=True, exist_ok=True)
    (podshows.STATE / "shows.json").write_text(json.dumps(registry))
    monkeypatch.setattr(podshows, "render", lambda: None)
    return monkeypatch


def session(monkeypatch, platform, user, name=""):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", platform)
    monkeypatch.setenv("HERMES_SESSION_USER_ID", user)
    monkeypatch.setenv("HERMES_SESSION_USER_NAME", name)


def shows(command, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["podshows.py", *command.split()])
    code = podshows.main()
    return code, capsys.readouterr().out.strip()


def test_one_person_many_identities_and_whatsapp_forms(people):
    assert users.find("whatsapp:440000000002@s.whatsapp.net") == "robin"
    assert users.find("whatsapp:+44 0000 000001") == "jo"
    assert users.find("telegram:111") == "jo"
    assert users.find("whatsapp:449999999999") == ""


def test_outside_a_chat_it_is_the_admin_and_a_stranger_becomes_a_member(people):
    assert users.current()[0] == "jo"
    session(people, "whatsapp", "449999999999@s.whatsapp.net", "Sam Sample")
    key, user, new = users.current()
    assert (key, user["role"], user["deliver"], new) == ("sam-sample", "member", "whatsapp:449999999999", True)
    assert users.current()[2] is False                                     # known from now on


def test_only_people_hermes_lets_in_are_sent_anything(people):
    with mock.patch.object(users, "hermes_setting", lambda name: "440000000002" if name == "WHATSAPP_ALLOWED_USERS" else ""), \
            mock.patch.object(users, "_hermes", lambda module: None):
        assert users.authorized("whatsapp:440000000002@s.whatsapp.net")
        assert not users.authorized("whatsapp:440000000003")
        assert not users.authorized("telegram:111")


def test_a_member_follows_and_the_admin_is_told(people, capsys):
    session(people, "whatsapp", "440000000002", "Robin")
    assert shows("follow Harbour Talk", people, capsys) == (0, "Harbour Talk: you follow it; each new episode's note comes to you here.")
    assert podshows.followers_of(podshows.show_for("1999999901")) == ["jo", "robin"]       # the admin followed it before
    assert notify.sent == [("telegram:111", "👤 Robin follows Harbour Talk")]
    assert shows("mine", people, capsys) == (0, "You follow: Harbour Talk.")
    assert shows("hosts Harbour Talk = A B", people, capsys)[0] == 1                        # the admin's to change
    assert shows("unfollow Harbour Talk", people, capsys)[1] == "Harbour Talk: you no longer follow it; it stays monitored for 1 other follower(s)."


def test_the_last_follower_leaving_stops_monitoring(people, capsys):
    assert shows("unfollow Harbour Talk", people, capsys)[1] == "Harbour Talk: you no longer follow it."
    assert podshows.followers_of(podshows.show_for("1999999901")) == [] and "1999999901" not in dict(podshows.monitored())
    assert notify.sent == []                                                            # the admin's own change


def test_a_member_queues_within_a_daily_limit(people):
    session(people, "whatsapp", "440000000003", "Kit")
    assert podqueue.add(APPLE, "chat").startswith("Queued (")
    assert "requested_by" not in podqueue.load_state()[APPLE]                # who asked lives in the request log
    assert users.requesters(APPLE) == ["kit"]
    assert notify.sent[-1][0] == "telegram:111" and notify.sent[-1][1].startswith("👤 Kit queued ")
    assert podqueue.add(APPLE.replace("001", "002"), "chat") == "Not queued: 1 episodes a day is the limit; try again tomorrow."


def test_a_finished_note_goes_to_whoever_queued_it_and_the_followers(people, tmp_path):
    with podshows.editing() as registry:
        registry["1999999901"]["followers"] = ["jo", "robin"]
    session(people, "whatsapp", "440000000003", "Kit")
    podqueue.add(APPLE, "chat")
    people.delenv("HERMES_SESSION_PLATFORM")
    notify.sent.clear()
    job = podqueue.CACHE / "jobs" / "podcast-1000000000001" / "source"
    job.mkdir(parents=True)
    (job / "resolved-episode-v1.json").write_text(json.dumps({"apple_show_id": "1999999901", "source_url": APPLE}))
    note = tmp_path / "Harbour Talk" / "2026-11-01 - Ep.md"
    note.parent.mkdir()
    note.write_text("---\nepisode: Ep\nlanguage: en\n---\n## Key themes\n\n### Tides\n- point\n")
    with mock.patch.object(podqueue, "run_podnote", return_value=(0, f"NOTE: {note}\nSTATUS: complete")), \
            mock.patch.object(podqueue, "run_verify", return_value=""), mock.patch.object(podqueue, "refresh_show"):
        message = podqueue.run()
    assert message.startswith("🟢 **Harbour Talk: Ep**") and "• **Tides**" in message   # the admin's, as cron output
    assert APPLE in message                                                              # the note's own link
    assert [target for target, _ in notify.sent] == ["whatsapp:440000000003", "whatsapp:440000000002"]
    assert all(text == message for _, text in notify.sent)


def test_a_failure_goes_to_everyone_who_asked(people):
    session(people, "whatsapp", "440000000002", "Robin")
    podqueue.add(APPLE, "chat")
    session(people, "whatsapp", "440000000003", "Kit")
    podqueue.add(APPLE, "chat")
    people.delenv("HERMES_SESSION_PLATFORM")
    notify.sent.clear()
    with mock.patch.object(podqueue, "run_podnote", return_value=(1, "STOPPED: YouTube says this video is private")), \
            mock.patch.object(podqueue, "run_verify", return_value=""):
        message = podqueue.run()
    assert message.startswith("🔴")
    assert notify.sent == [("whatsapp:440000000002", message), ("whatsapp:440000000003", message)]


def test_asking_for_an_episode_already_queued_joins_it_and_is_not_counted(people):
    session(people, "whatsapp", "440000000002", "Robin")
    podqueue.add(APPLE, "chat")
    session(people, "whatsapp", "440000000003", "Kit")                     # a limit of 1 a day
    notify.sent.clear()
    assert podqueue.add(APPLE, "chat") == "Already queued (position 1 of 1). You will get its note when it is made."
    assert users.requesters(APPLE) == ["robin", "kit"]
    assert notify.sent[-1][0] == "telegram:111" and notify.sent[-1][1].endswith("(queued already)")
    assert podqueue.add(APPLE.replace("001", "002"), "chat").startswith("Queued (")   # the join cost nothing
    assert users.requests_today("kit") == 1


def test_an_episode_already_made_is_sent_at_once(people, tmp_path):
    people.setattr(podqueue, "ROOT", tmp_path / "notes")
    people.setattr(podqueue, "EPISODES", Path("Podcasts"))
    folder = tmp_path / "notes" / "Podcasts" / "Harbour Talk"
    folder.mkdir(parents=True)
    (folder / "2026-11-01 - Ep.md").write_text("---\nepisode: Ep\napple_episode_id: '1000000000001'\nlanguage: en\n---\n"
                                               "## Key themes\n\n### Tides\n- **Neap** — low\n")
    session(people, "whatsapp", "440000000003", "Kit")
    assert podqueue.add(APPLE, "chat") == "Already in the library; not queued. Its summary is on its way to you."
    sent = dict(notify.sent)
    assert sent["whatsapp:440000000003"].startswith("🟢 **Harbour Talk: Ep**") and "– Neap" in sent["whatsapp:440000000003"]
    assert sent["telegram:111"].endswith("(made already; sent)")
    assert users.requests_today("kit") == 0 and users.requesters(APPLE) == ["kit"]


def test_a_blocked_user_gets_nothing_done(people):
    with users.editing() as everyone:
        everyone["robin"]["role"] = "blocked"
    session(people, "whatsapp", "440000000002", "Robin")
    assert podqueue.add(APPLE, "chat") == "Not queued: not available."


def test_the_admin_manages_people_and_nobody_else_can(people, capsys):
    import podusers

    def run(command):
        people.setattr(sys, "argv", ["podusers.py", *command.split()])
        return podusers.main(), capsys.readouterr().out.strip()
    code, listing = run("list")
    assert code == 0 and "Robin (member, 10 a day): whatsapp; follows 0 shows" in listing and "440000" not in listing
    assert run("limit kit = 3") == (0, "Kit: up to 3 episodes a day.")
    assert run("block Robin") == (0, "Robin: blocked.")
    assert run("identity robin = telegram:222") == (0, "Robin: now also on telegram.")
    assert users.find("telegram:222") == "robin"
    session(people, "whatsapp", "440000000003", "Kit")
    assert run("list") == (1, "Only Jo can see or change who uses this.")


def test_the_admin_subscribes_others_and_they_are_told(people, capsys):
    assert shows("follow Harbour Talk = Robin, Kit", people, capsys) == (0, "Harbour Talk: Robin, Kit now follow it.")
    assert podshows.followers_of(podshows.show_for("1999999901")) == ["jo", "robin", "kit"]
    assert notify.sent == [("whatsapp:440000000002", "Jo subscribed you to Harbour Talk: each new episode's note will come to you here."),
                           ("whatsapp:440000000003", "Jo subscribed you to Harbour Talk: each new episode's note will come to you here.")]
    assert shows("mine Robin", people, capsys) == (0, "Robin follows: Harbour Talk.")
    assert shows("followers Harbour Talk", people, capsys) == (0, "Harbour Talk: Jo, Robin, Kit.")
    assert shows("unfollow Harbour Talk = Kit", people, capsys) == (0, "Harbour Talk: Kit no longer follows it.")
    assert notify.sent[-1] == ("whatsapp:440000000003", "Jo unsubscribed you from Harbour Talk.")
    assert shows("follow Harbour Talk = Robin", people, capsys) == (0, "Harbour Talk: nothing to change.")


def test_a_member_cannot_subscribe_others_or_see_their_shows(people, capsys):
    session(people, "whatsapp", "440000000002", "Robin")
    for command in ("follow Harbour Talk = Kit", "unfollow Harbour Talk = Jo", "mine Kit", "followers Harbour Talk"):
        assert shows(command, people, capsys) == (1, "Only Jo can do that for someone else."), command
    assert podshows.followers_of(podshows.show_for("1999999901")) == ["jo"]


def test_following_a_youtube_only_show_watches_its_own_channel(people, capsys):
    with podshows.editing() as registry:
        registry["yt:UCanotherChannel00000000Cc"] = {"title": "Night Port", "folder": "Night Port", "country": "gb",
                                                     "hosts": [], "fixes": {}, "advertisers": {}}
    linked = []
    with mock.patch.object(podshows, "link_youtube",
                           lambda registry, key, link, source: linked.append(link) or "Night Port: linked to YouTube.") as _:
        code, said = shows("follow Night Port", people, capsys)
    assert code == 0 and said.endswith("Night Port: linked to YouTube.")
    assert linked == ["https://www.youtube.com/channel/UCanotherChannel00000000Cc"]


def test_a_link_s_own_equals_signs_never_split_a_command(people, capsys):
    assert podshows.assignment("Harbour Talk = Robin, Kit") == ("Harbour Talk", "Robin, Kit")
    assert podshows.assignment("https://www.youtube.com/@harbourtalk?si=AbC123 = Robin") == \
        ("https://www.youtube.com/@harbourtalk?si=AbC123", "Robin")
    assert podshows.assignment("https://www.youtube.com/playlist?list=PLexamplePlaylist0000000000000000A") == \
        ("https://www.youtube.com/playlist?list=PLexamplePlaylist0000000000000000A", "")
    assert podshows.assignment("kit=3") == ("kit", "3")
    with podshows.editing() as registry:
        podshows.set_youtube(registry["1999999901"], "1999999901",
                             {"channel": "UCexampleChannel000000Bb", "playlist": "", "handle": "@harbourtalk", "linked_by": "user"})
    assert shows("follow https://www.youtube.com/@harbourtalk?si=AbC123 = Robin", people, capsys) == \
        (0, "Harbour Talk: Robin now follows it.")


def test_a_member_s_language_never_overrides_the_show_s(people):
    with podshows.editing() as registry:
        registry["1999999901"]["language"] = "en"
        show = podshows.ensure_show(registry, "1999999901", "Harbour Talk",
                                    metadata={"language": "de", "language_source": "user"})
    assert show["language"] == "en"                                          # the admin's stays
    with podshows.editing() as registry:
        fresh = podshows.ensure_show(registry, "1999999902", "Night Port", metadata={"language": "de", "language_source": "user"})
    assert fresh["language"] == "de"                                         # a new show takes it


def test_a_wrong_name_changes_nothing_and_tells_nobody(people, capsys):
    with pytest.raises(SystemExit):
        shows("follow Harbour Talk = Robin, Nobody", people, capsys)
    assert podshows.followers_of(podshows.show_for("1999999901")) == ["jo"]
    assert notify.sent == []
    assert "robin" not in (podshows.STATE / "shows-changes.log").read_text() if (podshows.STATE / "shows-changes.log").exists() else True


def test_the_first_admin_is_named_at_a_shell_and_the_monitor_path_is_not_for_chats(people, capsys):
    import podusers
    people.setattr(sys, "argv", ["podusers.py", "admin", "Robin"])
    assert podusers.main() == 0 and users.load()["robin"]["role"] == "admin"
    people.setattr(sys, "argv", ["podqueue.py", "add", APPLE, "--source", "monitor"])
    assert podqueue.main() == 1 and "monitor's own" in capsys.readouterr().out


def test_whatsapp_gets_its_own_marks():
    assert notify.whatsapp_text("🟢 **Show: Ep**\n*Ann (host)*\n> A take.\n• **Tides**\n   – low") == \
        "🟢 *Show: Ep*\n_Ann (host)_\n> A take.\n• *Tides*\n   – low"
    assert notify.whatsapp_text("~~old~~ [Apple](https://a.b/c) `**code**` * not a list") == \
        "~old~ Apple (https://a.b/c) `**code**` * not a list"


def test_a_member_cannot_make_or_verify_a_note_directly(people, capsys):
    import podnote
    import podverify
    session(people, "whatsapp", "440000000002", "Robin")
    people.setattr(sys, "argv", ["podnote.py", "--job-dir", "/nowhere/job", "--force"])
    with mock.patch.object(podnote, "run") as run:
        assert podnote.main() == 1
    run.assert_not_called()
    assert capsys.readouterr().out.startswith("STOPPED: Only Jo can make or re-make a note directly")
    people.setattr(sys, "argv", ["podverify.py", "/nowhere/note.md"])
    with mock.patch.object(podverify, "verify") as verify:
        assert podverify.main() == 1
    verify.assert_not_called()
    session(people, "telegram", "111", "Jo")                                # the admin may
    people.setattr(sys, "argv", ["podnote.py", "--job-dir", "/nowhere/job", "--force"])
    with mock.patch.object(podnote, "run", return_value=0) as run:
        assert podnote.main() == 0
    run.assert_called_once()


def test_the_queue_runs_its_work_outside_the_chat_that_started_it(people):
    session(people, "whatsapp", "440000000002", "Robin")
    code, output = podqueue.run_owned([sys.executable, "-c", "import os; print(os.environ.get('HERMES_SESSION_PLATFORM', '-'))"], 30)
    assert (code, output.strip()) == (0, "-")


def test_an_episode_found_made_only_when_its_turn_comes_still_reaches_whoever_asked(people, tmp_path):
    session(people, "whatsapp", "440000000002", "Robin")
    podqueue.add(APPLE, "chat")
    people.delenv("HERMES_SESSION_PLATFORM")
    notify.sent.clear()
    note = tmp_path / "Harbour Talk" / "2026-11-01 - Ep.md"
    note.parent.mkdir()
    note.write_text("---\nepisode: Ep\nsource_url: https://www.youtube.com/watch?v=Xx0Test0Ep1\n---\n## Key themes\n\n### Tides\n")
    found = f"note already exists from another source (use --force to make this one too): {note}\nEXISTS: {note}\n"
    with mock.patch.object(podqueue, "run_podnote", return_value=(0, found)), mock.patch.object(podqueue, "run_verify"):
        assert podqueue.run() == ""                                                  # the admin saw it when it was made
    assert [target for target, _ in notify.sent] == ["whatsapp:440000000002"]
    assert notify.sent[0][1].startswith("🟢 **Harbour Talk: Ep**") and "watch?v=Xx0Test0Ep1" in notify.sent[0][1]
    assert podqueue.read_lines(podqueue.STATE / "queue.txt") == []


def test_a_failed_send_logs_neither_the_address_nor_the_message():
    import importlib
    real = importlib.reload(notify)                      # the real send, with Hermes and the clock mocked
    try:
        with mock.patch.object(real.users, "authorized", return_value=True), mock.patch.object(real.time, "sleep"), \
                mock.patch.object(real, "log") as log, mock.patch.object(real.subprocess, "run",
                    side_effect=real.subprocess.TimeoutExpired(["hermes", "send", "--to", "whatsapp:440000000002",
                                                                "private words"], 60)):
            assert real.send("whatsapp:440000000002", "private words", "Robin") is False
        logged = log.call_args.args[0]
        assert logged == "failed\tRobin\ttimed out"
        assert real.failure(1, ["no route to +44 0000 000002"], "whatsapp:440000000002", "hi") == "exit 1"
        assert real.failure(1, ["echo: …the rest of a private message"], "whatsapp:440000000002", "hi") == "exit 1"
    finally:
        importlib.reload(notify)


def test_a_limit_of_zero_means_none_and_a_missing_one_the_default():
    assert users.limit({"limit_per_day": 0}) == 0
    assert users.limit({}) == users.DAILY_LIMIT
    assert users.limit({"limit_per_day": 3}) == 3


def test_a_request_is_recorded_in_the_transaction_that_admits_the_episode(people):
    import store
    inside = []
    real = store.record_request

    def record(db, key, url, joined=False, at=""):
        inside.append(db.in_transaction and bool(store.queue(db)[0]))      # the episode is there, not yet committed
        real(db, key, url, joined, at)
    session(people, "whatsapp", "440000000003", "Kit")
    with mock.patch.object(store, "record_request", record):
        assert podqueue.add(APPLE, "chat").startswith("Queued (")
    assert inside == [True] and users.requesters(APPLE) == ["kit"]


def test_source_text_cannot_break_the_message_layout():
    message = podqueue.finished_message("Show", "Title\n[Open](https://example.invalid)", "A  B (host)", "take", [])
    assert message.splitlines()[0] == "🟢 **Show: Title [Open] (https://example.invalid)**"
    assert notify.whatsapp_text(message).splitlines()[0] == "🟢 *Show: Title [Open] (https://example.invalid)*"


def test_the_hermes_check_names_what_an_upgrade_broke(capsys):
    import types
    import check_hermes

    def formatted(*args, **kwargs):
        return format_message(args)                                          # Hermes formats these sends now
    adapter = types.SimpleNamespace(_standalone_send=formatted)
    modules = {"gateway.whatsapp_identity": None, "gateway.pairing": None, "plugins.platforms.whatsapp.adapter": adapter}
    with mock.patch.object(check_hermes.users, "_hermes", lambda name: modules.get(name)), \
            mock.patch.object(check_hermes, "send_options", return_value=""), \
            mock.patch.object(check_hermes, "session_variables", return_value=""):
        assert check_hermes.main() == 1
    out = capsys.readouterr().out
    assert "FAIL identity modules: missing: gateway.whatsapp_identity" in out
    assert "drop notify.whatsapp_text" in out and "ok   hermes send" in out


# ---- the store: outbox, import, commands

def outbox():
    import store
    with store.transaction() as db:
        return [dict(row) for row in db.execute("SELECT person, state, attempts, error FROM outbox ORDER BY id")]


def test_a_finished_note_s_messages_survive_a_crash_before_they_are_sent(people, tmp_path):
    with podshows.editing() as registry:
        registry["1999999901"]["followers"] = ["jo", "robin"]
    note = tmp_path / "Harbour Talk" / "2026-11-01 - Ep.md"
    note.parent.mkdir()
    note.write_text("---\nepisode: Ep\n---\n## Key themes\n\n### Tides\n")
    job = podqueue.CACHE / "jobs" / "podcast-1000000000001" / "source"
    job.mkdir(parents=True)
    (job / "resolved-episode-v1.json").write_text(json.dumps({"apple_show_id": "1999999901", "source_url": APPLE}))
    podqueue.add(APPLE, "monitor")
    with mock.patch.object(podqueue, "run_podnote", return_value=(0, f"NOTE: {note}\nSTATUS: complete")), \
            mock.patch.object(podqueue, "run_verify", return_value=""), mock.patch.object(podqueue, "refresh_show"), \
            mock.patch.object(podqueue, "deliver"):                               # the process dies before sending
        assert podqueue.run().startswith("🟢")
    assert podqueue.read_lines(podqueue.STATE / "queue.txt") == []              # finished and its message kept,
    assert [(row["person"], row["state"]) for row in outbox()] == [("robin", "pending")]   # in one commit
    notify.sent.clear()
    assert podqueue.deliver() == 1                                              # the next tick sends it
    assert [target for target, _ in notify.sent] == ["whatsapp:440000000002"]
    assert outbox()[0]["state"] == "sent"


def test_a_failed_send_is_tried_again_later_then_given_up(people):
    import store
    with store.transaction() as db:
        store.send_later(db, "robin", "hello")
    with mock.patch.object(notify, "send", return_value=False):
        podqueue.deliver()
        assert outbox()[0] == {"person": "robin", "state": "pending", "attempts": 1, "error": "failed"}
        podqueue.deliver()                                                      # not due yet: left alone
        assert outbox()[0]["attempts"] == 1
        for _ in podqueue.OUTBOX_WAITS:
            with store.transaction() as db:
                db.execute("UPDATE outbox SET next_try = '2000-01-01T00:00:00Z'")
            podqueue.deliver()
    assert outbox()[0]["state"] == "failed" and outbox()[0]["attempts"] == len(podqueue.OUTBOX_WAITS) + 1
    assert "1 failed" in podqueue.listing()


def test_a_send_cut_off_mid_way_is_tried_again_and_nowhere_to_send_is_not(people):
    import store
    with store.transaction() as db:
        store.send_later(db, "robin", "hello")
        store.send_later(db, "nobody", "hello")
        db.execute("UPDATE outbox SET state = 'sending', next_try = '2000-01-01T00:00:00Z' WHERE person = 'robin'")
    notify.sent.clear()
    assert podqueue.deliver() == 1
    assert [(row["person"], row["state"]) for row in outbox()] == [("robin", "sent"), ("nobody", "skipped")]


def test_the_files_of_before_are_imported_once_and_kept(people):
    import store
    second = APPLE.replace("001", "002")
    (podqueue.STATE / "queue.txt").write_text(f"{APPLE}\n{second}\n")
    (podqueue.STATE / "queue-state.json").write_text(json.dumps({APPLE: {"attempts": 2, "language": "en"}}))
    (users.STATE / "user-requests.log").write_text(f"2026-11-03T10:00:00Z\tkit\t{APPLE}\n"
                                                   f"2026-11-03T10:01:00Z\trobin\t{APPLE}\tjoined\n")
    assert podqueue.load_state()[APPLE] == {"attempts": 2, "language": "en"}
    assert users.requesters(APPLE) == ["kit", "robin"] and users.requests()[1][3] is True
    assert (podqueue.STATE / "queue.txt.before-store").read_text() == f"{APPLE}\n{second}\n"
    (podqueue.STATE / "queue.txt").write_text("")                                # later file changes: not imported again
    assert list(podqueue.load_state()) == [APPLE, second]
    with store.transaction() as db:
        assert "2 queued, 2 requests" in store.meta(db, "imported")


def test_the_queue_is_changed_by_command_and_only_by_the_admin(people, capsys):
    second = APPLE.replace("001", "002")
    podqueue.add(APPLE, "monitor")
    podqueue.add(second, "monitor")
    assert podqueue.move("2", 1) == f"Moved to position 1: {second}"
    assert podqueue.read_lines(podqueue.STATE / "queue.txt") == [second, APPLE]
    assert podqueue.remove(APPLE) == f"Removed: {APPLE}"
    assert list(podqueue.load_state()) == [second]
    session(people, "whatsapp", "440000000002", "Robin")
    people.setattr(sys, "argv", ["podqueue.py", "remove", "1"])
    assert podqueue.main() == 1
    assert "Only Jo can change the queue by hand" in capsys.readouterr().out
    assert list(podqueue.load_state()) == [second]


def test_export_writes_the_files_an_earlier_version_reads(people):
    session(people, "whatsapp", "440000000003", "Kit")
    podqueue.add(APPLE, "chat")
    people.delenv("HERMES_SESSION_PLATFORM")
    assert podqueue.export_files().startswith("Exported 1 queued and 1 requests")
    assert (users.STATE / "user-requests.log").read_text().split("\t")[1:] == ["kit", f"{APPLE}\n"]
    assert APPLE in json.loads((podqueue.STATE / "queue-state.json").read_text())


def test_an_overdue_verification_takes_its_turn_while_episodes_wait(people):
    note = podqueue.STATE / "n.md"
    note.write_text("---\n---\n")
    podqueue.add(APPLE, "monitor")
    late = podqueue.iso(podqueue.now() - podqueue.VERIFY_OVERDUE - podqueue.timedelta(minutes=1))
    (podqueue.STATE / "verify-retry.json").write_text(json.dumps({str(note): {"tries": 1, "next_try": late}}))
    with mock.patch.object(podqueue, "run_verify", return_value="Verified: 0 changes, 0 claims") as verify, \
            mock.patch.object(podqueue, "run_podnote") as podnote_run, mock.patch.object(podqueue, "refresh_show"):
        assert podqueue.run() == ""
    verify.assert_called_once_with(str(note))
    podnote_run.assert_not_called()                                              # the episode goes next tick
    assert podqueue.read_lines(podqueue.STATE / "queue.txt") == [APPLE]


def test_the_status_shows_only_what_needs_attention(people, capsys):
    import podcast_transcription
    import podstatus
    import store
    people.setattr(podcast_transcription, "runtime_problem", lambda *a: "")    # the speech runtime is ready
    people.setattr(sys, "argv", ["podstatus.py", "--problems"])
    assert podstatus.main() == 0 and capsys.readouterr().out == ""
    with store.transaction() as db:
        store.send_later(db, "robin", "hello")
        db.execute("UPDATE outbox SET state = 'failed', error = 'failed'")
    assert podstatus.main() == 1
    assert capsys.readouterr().out.strip() == "message to robin given up (failed)"
    people.setattr(sys, "argv", ["podstatus.py"])
    podstatus.main()
    assert capsys.readouterr().out.startswith("Queue: 0 queued. Messages: 0 waiting, 1 given up.")


# ---- second review (R01, R03, R08, R09)

def test_the_store_and_its_side_files_are_private_from_the_start(people):
    import os
    import store
    previous = os.umask(0o022)
    try:
        with store.transaction() as db:
            store.send_later(db, "robin", "private words")
            modes = {name: os.stat(store.STATE / name).st_mode & 0o777 for name in os.listdir(store.STATE)
                     if name.startswith("app.sqlite3")}
    finally:
        os.umask(previous)
    assert modes and set(modes.values()) == {0o600}, modes


def test_joining_an_episode_records_and_tells_in_the_admission_transaction(people):
    import store
    session(people, "whatsapp", "440000000002", "Robin")
    podqueue.add(APPLE, "chat")
    session(people, "whatsapp", "440000000003", "Kit")
    inside = []
    real_record, real_send = store.record_request, store.send_later
    people.setattr(store, "record_request", lambda db, *a, **k: inside.append(db.in_transaction) or real_record(db, *a, **k))
    people.setattr(store, "send_later", lambda db, *a, **k: inside.append(db.in_transaction) or real_send(db, *a, **k))
    assert podqueue.add(APPLE, "chat").startswith("Already queued")
    assert inside and all(inside)                                   # the request and the admin's notice, one commit


def test_any_podnote_failure_still_prints_a_result_line(people, capsys):
    import podnote
    import results
    people.setattr(sys, "argv", ["podnote.py", "--job-dir", "/nowhere/job"])
    with mock.patch.object(podnote, "run", side_effect=json.JSONDecodeError("Expecting value", "", 0)):
        assert podnote.main() == 1
    assert results.parse(capsys.readouterr().out)["STOPPED"].startswith("JSONDecodeError")


def test_a_busy_phrase_in_prose_does_not_keep_a_stopped_episode_queued(people):
    podqueue.add(APPLE, "monitor")
    output = "note: another podnote run is active elsewhere, ignored\nSTOPPED: this link is not an episode\n"
    with mock.patch.object(podqueue, "run_podnote", return_value=(1, output)), mock.patch.object(podqueue, "run_verify"):
        podqueue.run()
    assert podqueue.read_lines(podqueue.STATE / "queue.txt") == []
    assert "this link is not an episode" in (podqueue.STATE / "failed.txt").read_text()
