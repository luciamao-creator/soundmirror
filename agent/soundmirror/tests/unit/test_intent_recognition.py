"""
Intent Recognition Unit Tests
"""
import pytest

from soundmirror.director.intent_recognition import (
    detect_intent, intent_to_action, Intent
)


class TestBPMDetection:
    def test_detect_bpm_absolute(self):
        intent = detect_intent("BPM 88")
        assert intent is not None
        assert intent.name == "bpm_change"
        assert intent.parameters["bpm"] == 88
        assert intent.confidence >= 0.9

    def test_detect_bpm_lowercase(self):
        intent = detect_intent("bpm 120")
        assert intent is not None
        assert intent.name == "bpm_change"
        assert intent.parameters["bpm"] == 120

    def test_detect_bpm_chinese(self):
        intent = detect_intent("节奏 100")
        assert intent is not None
        assert intent.name == "bpm_change"

    def test_detect_bpm_change_to(self):
        intent = detect_intent("改成 88 BPM")
        assert intent is not None
        assert intent.name == "bpm_change"
        assert intent.parameters["bpm"] == 88

    def test_relative_bpm(self):
        intent = detect_intent("快一点")
        assert intent is not None
        assert intent.name == "bpm_change"
        assert intent.parameters["direction"] == "up"

    def test_unrealistic_bpm_rejected(self):
        intent = detect_intent("BPM 999")
        # 999 > 240, 不应被识别
        # 落到下一个 intent(skip/genre/etc)
        assert intent is None or intent.name != "bpm_change"


class TestKeyDetection:
    def test_detect_key_chinese(self):
        intent = detect_intent("降 B 大调")
        assert intent is not None
        assert intent.name == "key_change"

    def test_detect_key_english(self):
        intent = detect_intent("C minor")
        assert intent is not None
        assert intent.name == "key_change"

    def test_detect_key_simple(self):
        intent = detect_intent("D 调")
        # D 调 应该匹配到 key_change
        assert intent is not None
        assert intent.name == "key_change"


class TestGenreDetection:
    @pytest.mark.parametrize("text,expected_genre", [
        ("换成电子", "电子"),
        ("改成爵士", "爵士"),
        ("民谣风", "民谣"),
        ("hip-hop 一点", "嘻哈"),
        ("加点 r&b", "R&B"),
    ])
    def test_detect_genre(self, text, expected_genre):
        intent = detect_intent(text)
        assert intent is not None
        assert intent.name == "genre_change"
        assert intent.parameters["genre"] == expected_genre


class TestInstrumentDetection:
    @pytest.mark.parametrize("text,expected_instr", [
        ("加钢琴", "钢琴"),
        ("加吉他", "吉他"),
        ("加 bass", "贝斯"),
        ("加合成器", "合成器"),
    ])
    def test_detect_instrument(self, text, expected_instr):
        intent = detect_intent(text)
        assert intent is not None
        assert intent.name == "instrument_add"
        assert intent.parameters["instrument"] == expected_instr


class TestLyricEditDetection:
    def test_detect_line_edit(self):
        intent = detect_intent("第 3 句改一下")
        assert intent is not None
        assert intent.name == "lyric_edit"
        assert intent.parameters["target"] == "3"

    def test_detect_section_edit(self):
        intent = detect_intent("副歌改")
        assert intent is not None
        assert intent.name == "lyric_edit"
        assert intent.parameters["target"] == "副歌"

    def test_detect_bridge_edit(self):
        intent = detect_intent("bridge 改")
        assert intent is not None
        assert intent.name == "lyric_edit"
        assert "bridge" in intent.parameters["target"]


class TestStageControl:
    def test_detect_skip(self):
        intent = detect_intent("跳过")
        assert intent is not None
        assert intent.name == "skip_stage"

    def test_detect_next(self):
        intent = detect_intent("下一步")
        assert intent is not None
        assert intent.name == "skip_stage"

    def test_detect_back(self):
        intent = detect_intent("上一步")
        assert intent is not None
        assert intent.name == "back_stage"

    def test_detect_return(self):
        intent = detect_intent("返回")
        assert intent is not None
        assert intent.name == "back_stage"


class TestApproveReject:
    def test_detect_approve(self):
        intent = detect_intent("整体满意")
        assert intent is not None
        assert intent.name == "approve_review"

    def test_detect_yes(self):
        intent = detect_intent("确认")
        assert intent is not None
        assert intent.name == "confirm_yes"

    def test_detect_cancel(self):
        intent = detect_intent("取消")
        assert intent is not None
        assert intent.name == "reject_cancel"


class TestIntentToAction:
    def test_bpm_to_action(self):
        intent = Intent(name="bpm_change", parameters={"bpm": 88, "absolute": True})
        action = intent_to_action(intent)
        assert action["action_id"] == "variable.change"
        assert action["payload"]["name"] == "bpm"
        assert action["payload"]["value"] == 88

    def test_genre_to_action(self):
        intent = Intent(name="genre_change", parameters={"genre": "电子"})
        action = intent_to_action(intent)
        assert action["action_id"] == "variable.change"
        assert action["payload"]["value"] == "电子"

    def test_skip_to_action(self):
        intent = Intent(name="skip_stage", parameters={})
        action = intent_to_action(intent)
        assert action["action_id"] == "stage.skip"

    def test_unknown_intent_returns_none(self):
        intent = Intent(name="unknown_intent_xyz", parameters={})
        action = intent_to_action(intent)
        assert action is None


class TestEdgeCases:
    def test_empty_input(self):
        assert detect_intent("") is None
        assert detect_intent("   ") is None

    def test_unknown_input(self):
        # 完全没意义的文本
        assert detect_intent("xxxxxx") is None

    def test_priority_skip_over_bpm(self):
        # "跳过" 应被识别为 skip,不应该是 BPM
        intent = detect_intent("跳过")
        assert intent.name == "skip_stage"
