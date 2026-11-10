"""/hands whoami and /hands doctor: setup help that grants nothing. whoami is the only /hands command a non-owner
gets an answer from, shows only the sender's own id, only in a DM, and never makes anyone an owner."""
import pytest

from borrowed_hands import commands, runtime
from test_wiring import ctx_factory, src  # noqa: F401  (fixture)


def hands(c, args, source):
    return c.commands["hands"](args, source=source)


def test_a_stranger_in_a_dm_is_told_the_exact_owner_line_and_stays_a_stranger(ctx_factory):
    c = ctx_factory(owners=("telegram:1",))
    stranger = src(user="424242")
    reply = hands(c, "whoami", stranger)
    assert "`telegram:424242`" in reply and "not an owner" in reply
    assert "plugins.entries.borrowed-hands.settings.owners" in reply and "BH_OWNERS" in reply and "restart" in reply
    assert runtime.RT.settings.owners == ("telegram:1",)                      # nothing changed
    for sub in ("pair", "status", "doctor", "unpair x", "rotate-key"):        # and nothing else opened up
        assert hands(c, sub, stranger) == commands.NOT_OWNER


def test_an_owner_is_told_so(ctx_factory):
    c = ctx_factory(owners=("telegram:1",))
    reply = hands(c, "whoami", src(user="1"))
    assert "`telegram:1`" in reply and "an owner of Borrowed Hands" in reply and "not an owner" not in reply


@pytest.mark.parametrize("chat_type", ["group", "channel", "thread", None])
def test_whoami_outside_a_dm_shows_no_id(ctx_factory, chat_type):
    c = ctx_factory(owners=("telegram:1",))
    for user in ("1", "424242"):
        reply = hands(c, "whoami", src(user=user, chat_type=chat_type))
        assert reply == "Run /hands whoami in a direct message with me." and user not in reply.replace("/hands", "")


@pytest.mark.parametrize("user", ["`x`", "1 2", "a\nb", "*bold*", "[l](http://e)", "<@1>", "", "x" * 129, None, 424242, "424242\n", "\n424242"])
def test_an_id_with_markup_or_control_characters_is_not_echoed(ctx_factory, user):
    c = ctx_factory(owners=("telegram:1",))
    reply = hands(c, "whoami", src(user=user))
    assert reply.startswith("I cannot show who you are") and "`" not in reply
    if user:
        assert str(user) not in reply


def test_with_no_owner_configured_everyone_is_pointed_at_whoami(ctx_factory):
    c = ctx_factory(owners=())
    anyone = src(user="7")
    for sub in ("pair", "status", "doctor", "unpair x"):
        assert hands(c, sub, anyone) == commands.NO_OWNER
    assert "/hands whoami" in commands.NO_OWNER
    assert "`telegram:7`" in hands(c, "whoami", anyone)


def test_the_local_operator_needs_no_id(ctx_factory):
    c = ctx_factory(owners=())
    assert "local:cli" in hands(c, "whoami", None)


def test_doctor_is_status_for_the_owner(ctx_factory):
    c = ctx_factory(owners=("telegram:1",))
    owner = src(user="1")
    doctor, status = hands(c, "doctor", owner), hands(c, "status", owner)
    assert doctor == status and "Listener:" in doctor
    assert "/hands doctor" in commands.USAGE and "/hands whoami" in commands.USAGE
