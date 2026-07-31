from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from Module.crawler import BoundedSet, DCInsideCrawler
from Module.retry_policy import RetryPolicy


def make_post_row(title: object, post_id: object, has_image: object=False, notice: object=False) -> object:
    icon = '<em class="icon_img icon_pic"></em>' if has_image else ""
    data_type = ' data-type="icon_notice"' if notice else ""
    return f"""
    <tr class="ub-content us-post"{data_type}>
        <td class="gall_tit ub-word">
            <a href="/mgallery/board/view/?id=test&no={post_id}">{title}</a>{icon}
        </td>
    </tr>
    """


def make_list_html(rows: object) -> object:
    return f"<html><body><table><tbody>{''.join(rows)}</tbody></table></body></html>"


def make_safety_rows() -> list[object]:
    return [make_post_row(f"pending{i}", i, has_image=True) for i in range(20)]


def make_crawler(html: object) -> object:
    crawler = DCInsideCrawler("https://gall.dcinside.com/mgallery/board/lists/?id=test")
    response = MagicMock()
    response.text = html
    response.raise_for_status = MagicMock()
    crawler.session = MagicMock()
    crawler.session.get.return_value = response
    return crawler


class TestBoundedSet:
    def test_add_and_contains(self) -> None:
        s = BoundedSet(maxsize=3)
        s.add("a")
        assert "a" in s
        assert "b" not in s

    def test_evicts_oldest_when_full(self) -> None:
        s = BoundedSet(maxsize=2)
        s.add("a")
        s.add("b")
        s.add("c")
        assert "a" not in s
        assert "b" in s
        assert "c" in s

    def test_readd_moves_to_end(self) -> None:
        s = BoundedSet(maxsize=2)
        s.add("a")
        s.add("b")
        s.add("a")  # a를 최신으로 갱신
        s.add("c")  # b가 제거되어야 함
        assert "a" in s
        assert "b" not in s

    def test_clear(self) -> None:
        s = BoundedSet(maxsize=2)
        s.add("a")
        s.clear()
        assert "a" not in s


class TestGetLatestPost:
    def test_skips_20_normal_posts_for_moderation_safety(self) -> None:
        rows = [make_post_row("notice", 999, has_image=True, notice=True)]
        rows += make_safety_rows()
        rows += [make_post_row("safe", 20, has_image=True)]
        crawler = make_crawler(make_list_html(rows))

        post = crawler.get_latest_post()

        assert post is not None
        assert post["title"] == "safe"
        assert post["link"].endswith("no=20")
        assert post["post_id"] == "20"
        assert post["has_image"] is True

    def test_detects_post_without_image(self) -> None:
        rows = make_safety_rows() + [make_post_row("post", 20, has_image=False)]
        crawler = make_crawler(make_list_html(rows))

        post = crawler.get_latest_post()

        assert post is not None
        assert post["has_image"] is False

    def test_does_not_return_same_post_twice(self) -> None:
        rows = make_safety_rows()
        rows += [make_post_row("same title", 20, has_image=True)]
        rows += [make_post_row("same title", 21, has_image=True)]
        crawler = make_crawler(make_list_html(rows))

        first = crawler.get_latest_post()
        crawler.mark_sent(first["post_id"])
        second = crawler.get_latest_post()

        assert first["post_id"] == "20"
        assert second["post_id"] == "21"

    def test_ignores_external_and_non_post_links(self) -> None:
        rows = make_safety_rows() + [
            '<tr class="ub-content"><td class="gall_tit"><a href="https://evil.example/board/view/?no=1">bad</a></td></tr>',
            '<tr class="ub-content"><td class="gall_tit"><a href="/mgallery/board/lists/?id=test">list</a></td></tr>',
            make_post_row("post", 20, has_image=True),
        ]
        crawler = make_crawler(make_list_html(rows))

        assert crawler.get_latest_post()["post_id"] == "20"

    def test_returns_none_when_no_posts(self) -> None:
        crawler = make_crawler("<html><body></body></html>")
        assert crawler.get_latest_post() is None

    def test_returns_none_on_request_error(self) -> None:
        import requests

        crawler = make_crawler("")
        crawler.retry_policy = RetryPolicy(base_delay=0)
        crawler.session.get.side_effect = requests.ConnectionError("boom")
        assert crawler.get_latest_post() is None

    def test_retries_transient_page_error(self) -> None:
        import requests

        crawler = make_crawler(
            make_list_html(make_safety_rows() + [make_post_row("safe", 20, True)])
        )
        success = crawler.session.get.return_value
        unavailable = MagicMock(status_code=503, headers={}, text="")
        unavailable.raise_for_status.side_effect = requests.HTTPError(
            response=unavailable
        )
        crawler.session.get.side_effect = [unavailable, success]
        crawler.retry_policy = RetryPolicy(base_delay=0)

        assert crawler.get_latest_post()["post_id"] == "20"
        assert crawler.session.get.call_count == 2


@pytest.mark.parametrize("has_image", [True, False])
def test_image_check(has_image: object) -> None:
    from bs4 import BeautifulSoup

    html = make_post_row("t", 1, has_image=has_image)
    row = BeautifulSoup(html, "html.parser").select_one("tr")
    crawler = DCInsideCrawler("https://example.com")
    assert crawler.image_check(row) is has_image
