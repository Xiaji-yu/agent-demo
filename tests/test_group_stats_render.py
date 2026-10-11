"""发言榜卡片 / 词云 / 个人周报卡片渲染测试。

来源：群发言榜+词云 AC 计划 §5-C/D 与榜单点评 skill AC 计划 §7-D。字体探测
依赖真实环境（无 CJK 字体时整体 skip，与 test_overview_render 同款）；几何性质
用像素级断言锁死——布局算法（等环间距螺旋、墨迹紧盒、墨阶配色、动态高度）
改坏必须在这里红。
"""

import io

import pytest

from agentcore.render import cloud, fonts, rank, review
from agentcore.render.cloud import (
    _font_size,
    _place,
    _text_layer,
    render_cloud_png,
)
from agentcore.render.rank import RankRow, render_rank_png
from agentcore.render.review import render_review_png

pytestmark = pytest.mark.skipif(
    not fonts.ensure_font_probed(), reason="环境无 CJK 字体，跳过渲染测试"
)


def _png_size(data: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        return im.size


class TestRankCard:
    def _rows(self):
        return [
            RankRow(1, "福建省最大榄角", 357, 38.0, is_new=True),
            RankRow(2, "自分が思うより", 148, 16.0, delta=2),
            RankRow(3, "Chen", 134, 14.0),
            RankRow(4, "Swaggy", 83, 9.0, is_new=True),
            RankRow(5, "你是金铲铲冠冕", 63, 7.0, delta=-2),
        ]

    def test_render_basic(self):
        png = render_rank_png(
            "周榜 | 10/5 ~ 10/11",
            self._rows(),
            total=1757,
            peak_text="19:00 前后（430 条）",
        )
        assert png is not None
        w, h = _png_size(png)
        assert w == rank.W
        # 高度随行数变化：5 行必须比 3 行高（动态高度没退化成固定值）
        png3 = render_rank_png("t", self._rows()[:3], total=100)
        assert _png_size(png3)[1] < h

    def test_empty_rows_returns_none(self):
        assert render_rank_png("周榜", [], total=0) is None

    def test_long_nickname_truncated_not_overflow(self):
        rows = [RankRow(1, "超" * 200, 10, 100.0)]
        png = render_rank_png("周榜", rows, total=10)
        assert png is not None  # 截断后仍画得出（不抛异常、不越界）

    def test_delta_and_new_variants_render(self):
        rows = [
            RankRow(1, "甲", 10, 50.0, delta=0),  # 平
            RankRow(2, "乙", 8, 40.0, delta=5),  # 升
            RankRow(3, "丙", 6, 30.0, delta=-3),  # 降
            RankRow(4, "丁", 4, 20.0, is_new=True),  # 新上榜
        ]
        assert render_rank_png("周榜", rows, total=20) is not None

    def test_render_exception_swallowed_to_none(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("draw failed")

        monkeypatch.setattr(rank, "_render", boom)
        assert render_rank_png("周榜", self._rows(), total=1) is None


class TestCloud:
    WORDS = [
        ("服务器", 120),
        ("老吴", 100),
        ("机器人", 90),
        ("显卡", 80),
        ("合并转发", 70),
        ("笑话", 60),
        ("深夜", 50),
        ("奶茶", 40),
        ("早八", 30),
        ("摸鱼", 20),
    ]

    def test_render_basic(self):
        png = render_cloud_png("群聊词云 · 10/5 ~ 10/11", self.WORDS)
        assert png is not None
        assert _png_size(png) == (cloud.W, cloud.H)

    def test_empty_words_returns_none(self):
        assert render_cloud_png("群聊词云", []) is None

    def test_max_words_cap(self):
        many = [(f"词{i}", 100 - i) for i in range(200)]
        png = render_cloud_png("群聊词云", many, max_words=5)
        assert png is not None  # 只放前 5 个也画得出
        # 200 个词全部争放时不该把渲染拖垮（等环间距螺旋 + 半径上限早退）
        png_all = render_cloud_png("群聊词云", many)
        assert png_all is not None

    def test_deterministic_same_input_same_png(self):
        a = render_cloud_png("群聊词云", self.WORDS)
        b = render_cloud_png("群聊词云", self.WORDS)
        assert a == b  # 无随机数：同输入必须同输出（用户重开看到跳动的布局
        # 会以为统计变了；测试也才能断言几何性质）

    def test_no_overlap_between_placed_words(self):
        """放置成功的词两两不重叠（碰撞盒语义）。"""
        maxc = max(c for _, c in self.WORDS)
        boxes = []
        for idx, (word, count) in enumerate(self.WORDS):
            angle = cloud._ANGLES[idx % len(cloud._ANGLES)]
            layer, w, h = _text_layer(word, _font_size(count, maxc), angle)
            pos = _place(boxes, w, h, idx)
            assert pos is not None, f"{word} 应放得下（小词表不该丢词）"
            boxes.append((pos[0], pos[1], pos[0] + w, pos[1] + h))
        for i, (ax0, ay0, ax1, ay1) in enumerate(boxes):
            for bx0, by0, bx1, by1 in boxes[i + 1 :]:
                assert not (ax0 < bx1 and ax1 > bx0 and ay0 < by1 and ay1 > by0), (
                    f"词 {self.WORDS[i][0]} 与后续词重叠"
                )

    def test_all_words_within_canvas(self):
        maxc = max(c for _, c in self.WORDS)
        boxes = []
        for idx, (word, count) in enumerate(self.WORDS):
            angle = cloud._ANGLES[idx % len(cloud._ANGLES)]
            layer, w, h = _text_layer(word, _font_size(count, maxc), angle)
            pos = _place(boxes, w, h, idx)
            assert pos is not None
            x, y = pos
            assert 0 <= x and x + w <= cloud.W
            assert 0 <= y and y + h <= cloud.H
            boxes.append((x, y, x + w, y + h))

    def test_font_size_monotonic_with_count(self):
        assert _font_size(100, 100) > _font_size(50, 100) > _font_size(10, 100)
        assert _font_size(100, 100) == cloud.MAX_SIZE
        assert _font_size(0, 100) >= cloud.MIN_SIZE
        assert _font_size(5, 0) == cloud.MIN_SIZE  # 全零词频不崩

    def test_ink_tight_box_not_font_metrics(self):
        """墨迹紧盒：层高必须显著小于 getmetrics 的 ascent+descent（wqy ≈ 2em）。

        锁死初版 bug：用字体度量当碰撞盒，40 词只放下 11 个。
        """
        layer, w, h = _text_layer("服务器", 60, 0)
        from agentcore.render.fonts import font as _font

        ascent, descent = _font(60).getmetrics()
        assert h < (ascent + descent) * 0.8

    def test_place_returns_none_when_full(self):
        """画布被一个超大盒占满时，任何词都放不下 → None（调用方走文案）。"""
        full = [(0, 0, cloud.W, cloud.H)]
        assert _place(full, 100, 50, 0) is None

    def test_render_exception_swallowed_to_none(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("draw failed")

        monkeypatch.setattr(cloud, "_render", boom)
        assert render_cloud_png("群聊词云", self.WORDS) is None

    def test_no_font_returns_none(self, monkeypatch):
        monkeypatch.setattr(fonts, "_font_probe_done", True)
        monkeypatch.setattr(fonts, "_font_ok", False)
        assert render_cloud_png("群聊词云", self.WORDS) is None


class TestReviewCard:
    _FACTS = {
        "msgs": 87,
        "chars": 640,
        "rank": 3,
        "users": 512,
        "days": 6,
        "peak_text": "23:00 前后（12 条）",
    }

    def test_render_basic(self):
        png = render_review_png(
            "我的群周报 | 10/5 ~ 10/11",
            self._FACTS,
            "你这周相当活跃，深夜十一点还在奋战，服务器话题没少聊。",
            words=["服务器", "显卡", "奶茶"],
        )
        assert png is not None
        assert _png_size(png)[0] == review.W

    def test_height_grows_with_comment(self):
        short = render_review_png("t", self._FACTS, "短评。")
        long = render_review_png("t", self._FACTS, "评" * 200)
        assert _png_size(short)[1] < _png_size(long)[1]

    def test_empty_comment_is_facts_card(self):
        png = render_review_png("t", self._FACTS, "")
        assert png is not None  # 纯事实卡（评语生成失败时的降级形态）

    def test_empty_facts_returns_none(self):
        assert render_review_png("t", {}, "评语") is None
        assert render_review_png("t", {"msgs": 0}, "评语") is None

    def test_missing_fields_dont_crash(self):
        """事实缺键（rank/users/peak 全缺）也要画得出（占位 —）。"""
        png = render_review_png("t", {"msgs": 5}, "加油。")
        assert png is not None

    def test_long_words_truncated_not_overflow(self):
        png = render_review_png(
            "t", self._FACTS, "评语", words=[f"词{i}" * 6 for i in range(20)]
        )
        assert png is not None  # 一行放不下就截断，不换行不溢出

    def test_words_label_own_vs_group(self):
        """有本人高频词时标签为「你的高频词」，否则「群话题」——两条路都画得出。"""
        png_own = render_review_png(
            "t", self._FACTS, "评语", words=["服务器"], words_label="你的高频词"
        )
        png_group = render_review_png("t", self._FACTS, "评语", words=["服务器"])
        assert png_own is not None and png_group is not None
        assert png_own == render_review_png(
            "t", self._FACTS, "评语", words=["服务器"], words_label="你的高频词"
        )

    def test_deterministic(self):
        a = render_review_png("t", self._FACTS, "同样的评语", words=["服务器"])
        b = render_review_png("t", self._FACTS, "同样的评语", words=["服务器"])
        assert a == b

    def test_wrap_cjk_greedy(self):
        from PIL import Image, ImageDraw

        from agentcore.render.fonts import font as _font

        probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
        lines = review._wrap(probe, "评" * 100, _font(18), 300)
        assert len(lines) > 1
        assert all(probe.textlength(line, font=_font(18)) <= 300 for line in lines)
        assert "".join(lines) == "评" * 100  # 不丢字

    def test_render_exception_swallowed_to_none(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("draw failed")

        monkeypatch.setattr(review, "_render", boom)
        assert render_review_png("t", self._FACTS, "评语") is None

    def test_no_font_returns_none(self, monkeypatch):
        monkeypatch.setattr(fonts, "_font_probe_done", True)
        monkeypatch.setattr(fonts, "_font_ok", False)
        assert render_review_png("t", self._FACTS, "评语") is None
