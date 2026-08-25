"""_sanitize_blocks (signals_blocks.py) -- the backstop against Slack's invalid_blocks
failure mode, where one over-length field fails the whole message, not just the
offending block. Real incident, 2026-08-25: a startup morning report never posted
because a per-node Stop button's dynamically-built label exceeded Slack's 75-char
button text cap.
"""
import signals_blocks as sb


def test_oversized_button_text_gets_clipped():
    blocks = [{
        "type": "actions",
        "elements": [{
            "type": "button", "style": "danger",
            "text": {"type": "plain_text",
                      "text": "🛑 Stop SOMETICKER (already halted: account 'brokerage' is dry-run "
                               "(no real orders placed))"},
            "action_id": "stop_node_automation",
        }],
    }]
    out = sb._sanitize_blocks(blocks)
    label = out[0]["elements"][0]["text"]["text"]
    assert sb._utf16_len(label) <= sb._BUTTON_TEXT_LIMIT
    assert label.endswith("…")


def test_short_button_text_untouched():
    text = "▶️ Start Engine"
    blocks = [{"type": "actions", "elements": [
        {"type": "button", "text": {"type": "plain_text", "text": text}, "action_id": "x"}
    ]}]
    out = sb._sanitize_blocks(blocks)
    assert out[0]["elements"][0]["text"]["text"] == text


def test_oversized_confirm_fields_get_clipped():
    blocks = [{
        "type": "actions",
        "elements": [{
            "type": "button", "text": {"type": "plain_text", "text": "Stop"}, "action_id": "x",
            "confirm": {
                "title": {"type": "plain_text", "text": "T" * 200},
                "text": {"type": "mrkdwn", "text": "X" * 500},
                "confirm": {"type": "plain_text", "text": "Y" * 50},
                "deny": {"type": "plain_text", "text": "Z" * 50},
            },
        }],
    }]
    out = sb._sanitize_blocks(blocks)
    confirm = out[0]["elements"][0]["confirm"]
    assert sb._utf16_len(confirm["title"]["text"]) <= sb._CONFIRM_TITLE_LIMIT
    assert sb._utf16_len(confirm["text"]["text"]) <= sb._CONFIRM_TEXT_LIMIT
    assert sb._utf16_len(confirm["confirm"]["text"]) <= sb._CONFIRM_BUTTON_LIMIT
    assert sb._utf16_len(confirm["deny"]["text"]) <= sb._CONFIRM_BUTTON_LIMIT


def test_none_and_empty_blocks_pass_through():
    assert sb._sanitize_blocks(None) is None
    assert sb._sanitize_blocks([]) == []


def test_unexpected_shape_does_not_raise():
    # Malformed/unexpected block shapes must never crash the send path over
    # a cosmetic guard -- best-effort clip, never raise.
    blocks = [{"type": "actions", "elements": [{"type": "button"}]}, {"type": "weird"}, "not-a-dict"]
    sb._sanitize_blocks(blocks)  # should not raise


def test_ticker_block_emits_separate_warning_context_before_stop_button(monkeypatch):
    """2026-08-25 redesign, real regression coverage (H1 from the paired
    contextual review: the previous version of this test built its own
    f-string locally and asserted it equals itself -- a tautology that
    would stay green even if a dynamic suffix were reintroduced onto the
    button at signals_blocks.py's real Stop-button call site). Drives the
    real _ticker_block function end to end instead."""
    import signals_config as cfg
    monkeypatch.setattr(cfg, "INTERACTIVE", True)
    monkeypatch.setattr(sb, "automation_blockers_other_than_node",
                         lambda ticker, account=None: ["account 'brokerage' is dry-run "
                                                         "(no real orders placed, a long real "
                                                         "blocker description)"])
    monkeypatch.setattr(sb.schwab_safety, "node_automation_enabled", lambda wl_id: True)

    row = {
        "Ticker": "SOMELONGTICKER", "Version": "v6", "Account": "brokerage",
        "Proximity": 12.0, "Next Action": "WAIT", "Phase": None,
        "Now": 100.0, "Next Trigger $": 110.0, "Held": False,
        "Overnight %": 0.0, "TrailBuy%": 3.0, "Arm%": 30.0, "TrailSell%": 1.0,
        "Last Sale $": 5000.0, "Z Trigger": None, "Trigger Label": "trig", "Z": -1.0,
        "State": "live",
        "_node": {"id": 999, "state": "live", "account": "brokerage", "version": "v6"},
    }
    blocks = sb._ticker_block(row)

    context_idx = next((i for i, b in enumerate(blocks)
                         if b.get("type") == "context"
                         and "⚠️" in (b["elements"][0].get("text") or "")), None)
    actions_idx = next((i for i, b in enumerate(blocks) if b.get("type") == "actions"), None)
    assert context_idx is not None, "no ⚠️ warning context block was emitted"
    assert actions_idx is not None, "no actions block (Stop button) was emitted"
    assert context_idx < actions_idx, "warning must render before the Stop button, not after"

    stop_btn = next(el for el in blocks[actions_idx]["elements"]
                     if el.get("action_id") == "stop_node_automation")
    assert stop_btn["text"]["text"] == "🛑 Stop SOMELONGTICKER"  # exact -- no dynamic suffix, ever


def test_utf16_len_counts_astral_emoji_as_two_units():
    # 🛑 (U+1F6D1) is a single Python code point but 2 UTF-16 code units --
    # confirms the fix for the emoji-counting gap the paired review found.
    assert len("🛑") == 1
    assert sb._utf16_len("🛑") == 2
