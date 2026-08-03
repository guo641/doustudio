"""v0.2.11:prompt 段标记解析单测。"""
from doupool.prompt_parser import split_by_segment_markers


def test_no_marker_returns_single_segment():
    """v0.2.11:无标记整段当一个 prompt(用户原文不被错切)。"""
    text = "一只橘猫在阳光下打滚,镜头慢慢拉近"
    assert split_by_segment_markers(text) == [text]


def test_no_marker_with_line_breaks_returns_single_segment():
    """v0.2.11:无标记的多行文字也整段当一个。"""
    text = "第一行\n第二行\n第三行"
    assert split_by_segment_markers(text) == [text]


def test_first_second_third_chinese_with_colon():
    text = "第一段:猫 第二段:狗 第三段:鱼"
    # 注意:同行两段(无换行)只切第一个,后段保留在第一段里(避免误伤)
    assert split_by_segment_markers(text) == ["猫 第二段:狗 第三段:鱼"]


def test_first_second_on_different_lines():
    text = "第一段:猫\n第二段:狗"
    assert split_by_segment_markers(text) == ["猫", "狗"]


def test_segment_n_form():
    text = "段一:猫\n段二:狗"
    assert split_by_segment_markers(text) == ["猫", "狗"]


def test_arabic_numbered_with_dot():
    text = "1. 猫\n2. 狗\n3. 鱼"
    assert split_by_segment_markers(text) == ["猫", "狗", "鱼"]


def test_arabic_numbered_with_dunhao_and_paren():
    text = "1、 猫\n2、 狗\n3) 鱼"
    assert split_by_segment_markers(text) == ["猫", "狗", "鱼"]


def test_mixed_markers_on_different_lines():
    text = "第一段:猫\n段二:狗\n3. 鱼"
    assert split_by_segment_markers(text) == ["猫", "狗", "鱼"]


def test_marker_inside_text_does_not_split():
    """v0.2.11:行内出现「第一段」字眼不切(避免误伤 prompt 文本)。"""
    text = "场景中描述 第一段 是文案,然后继续"
    assert split_by_segment_markers(text) == [text]


def test_empty_returns_empty_list():
    assert split_by_segment_markers("") == []
    assert split_by_segment_markers("   ") == []
    assert split_by_segment_markers(None) == []


def test_marks_with_fullwidth_colon():
    text = "第一段:猫\n第二段:狗"
    assert split_by_segment_markers(text) == ["猫", "狗"]


def test_single_marker_with_content():
    text = "第一段:一只孤独的狼在雪地行走"
    assert split_by_segment_markers(text) == ["一只孤独的狼在雪地行走"]


def test_line_leading_whitespace():
    text = "   第一段:猫\n  第二段:狗"
    assert split_by_segment_markers(text) == ["猫", "狗"]


def test_skip_number():
    """v0.2.11:跳号也支持(用户写第一段→第三段不会报错)。"""
    text = "第一段:猫\n第三段:鱼"
    assert split_by_segment_markers(text) == ["猫", "鱼"]


def test_large_chinese_number():
    """v0.2.11:支持第十、第十一、第十二这种复合数字。"""
    text = "第十段:猫\n第十二段:狗"
    assert split_by_segment_markers(text) == ["猫", "狗"]


def test_marker_only_no_content_returns_marker_as_segment():
    """v0.2.11:光秃秃的标记(如 '第一段')返回它本身(让上层过滤空)。"""
    text = "第一段"
    assert split_by_segment_markers(text) == ["第一段"]


def test_unknown_glyph_as_number_does_not_match():
    """v0.2.11:非序数词的字符不能被当成数字段标记。"""
    text = "第N段:猫"  # N 不是数字也不是中文序数词
    assert split_by_segment_markers(text) == [text]


def test_marker_without_colon_still_splits():
    """v0.2.11:标记后没冒号也切(用户口语写法)。"""
    text = "第一段 猫\n第二段 狗"
    assert split_by_segment_markers(text) == ["猫", "狗"]


def test_arabic_no_space_after_dot():
    text = "1.猫\n2.狗"
    assert split_by_segment_markers(text) == ["猫", "狗"]