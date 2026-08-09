"""디스코드 이미지 임베드 생성 공통 헬퍼.

dcbot / arca_bot / message_sender 가 모두 동일한
`Embed(title, url, color) + set_image(attachment://...)` 패턴을 쓰므로 한 곳으로 모은다.
"""
from __future__ import annotations

import discord


def make_image_embed(
    filename: str,
    *,
    title: str | None = None,
    url: str | None = None,
    color: int,
    footer: str | None = None,
) -> discord.Embed:
    """첨부 이미지를 보여주는 임베드를 만든다.

    title/url이 있으면 제목이 하이퍼링크가 되고, footer가 있으면 하단에 표시한다.
    """
    embed = discord.Embed(title=title, url=url, color=color)
    if footer:
        embed.set_footer(text=footer)
    embed.set_image(url=f"attachment://{filename}")
    return embed
