# Gallery Image Relay 리팩터링 설계

## 1. 배경과 목표

2026-08-09 코드 품질 점검과 PR #111의 QR-001~005 반영 후 코드 품질 및 구조 점수는 각각 8/10이다.
현재 서비스는 안정적으로 동작하지만 다음 구조적 부채가 남아 있다.

- 전송 성공 상태가 `bool`, `(item, resolved)`, `(items, all_resolved)`처럼 서로 다른 형태로 전달된다.
- DCInside와 Arcalive가 Discord 채널 fan-out, embed 생성, 413 fallback, hash 확정을 서로 다른 경로에서 수행한다.
- `Module/message_sender.py`, bot/crawler, `launcher.py`, `web_app.py` 등에 광범위한 `object` annotation이 남아 있다.
- `ImageHandler.is_duplicate()`는 이미 deprecated지만 호환 테스트와 API가 유지되고 있다.

목표는 아래 세 가지다.

1. 전송 결과를 명시적이고 관찰 가능한 typed result로 통일한다.
2. DC 단일 이미지와 Arca batch의 Discord fan-out을 하나의 경로로 합친다.
3. crawler, orchestration, transport, archive 경계를 드러내고 구체 타입으로 계약을 고정한다.

이번 리팩터링은 구조만 바꾸며 Discord/Telegram 메시지 형식, retry 결과, post/hash ack 시점,
웹 endpoint 및 설정 기본값을 포함한 외부 동작은 바꾸지 않는다.

## 2. 현재 구조 요약

```text
DCInsideCrawler / ArcaliveCrawler
        -> DCBot / ArcaBot
        -> ImageHandler
        -> MediaPipeline
             -> MessageSender -> Discord / Telegram
             -> GalleryClient -> RAM-only web gallery
        -> DeliveryArchive -> SQLite metadata ledger
```

`Module/crawler.py`와 `Module/arca_crawler.py`는 새 post를 고르고 `post_id` 중복 여부를 확인한다.
bot은 이미지를 내려받아 압축·검증한 뒤 `MediaPipeline`에 넘기고, 성공한 경우에만
`mark_hash_sent()` 및 crawler의 `mark_sent()`를 호출한다.

| 구분 | DCInside | Arcalive |
| --- | --- | --- |
| orchestration | `Module/dcbot.py::process_post()` | `Module/arca_bot.py::process_post()` |
| 이미지 단위 | post당 단일 이미지가 기본 | post당 최대 4개, message당 최대 10개 |
| Discord 경로 | `MediaPipeline.distribute()` -> `send_single_to_channels()` | `_send_image_batch()` -> `send_batch_to_channel()` |
| Telegram | 사용 | 사용하지 않음 |
| Discord 413 | `MessageSender.send_to_discord()`에서 재압축 후 1회 재시도 | `ArcaBot._send_fallback()`에서 개별 전송/재압축 |
| post ack | Discord 또는 Telegram 중 하나라도 성공 | 한 channel/batch라도 성공하고 모든 download가 resolved |

`Module/delivery_archive.py`는 `(source, gallery_name, delivery_key)`와 `delivered_at`만 SQLite에 기록한다.
이미지 byte는 저장하지 않으며 post key와 SHA256 image key를 분리해 restart 후 중복 전송을 막는다.

PR #112는 `MediaPipeline.source_label`을 도입해 단일/배치 footer를
`디시인사이드 · 1개 이미지`, `아카라이브 · N개 이미지` 형태로 공통화했다.
이는 source별 값만 주입하고 전송 조립은 공통 경로가 담당하는 consolidation의 시작점이다.

## 3. 확정 결정 사항

다음은 설계 선택지가 아니라 owner(tae)가 2026-08-09 확정한 불변 조건이다.

1. **Delivery acknowledgement는 현재의 any-destination semantics를 유지한다.**
   fan-out 대상 중 하나라도 성공하면 성공으로 간주하고, 실패한 destination을 독립적으로 재시도하지 않는다.
   typed result가 destination별 실패를 기록하더라도 ack 판정은 바꾸지 않는다.
2. **대형 리팩터링을 허용한다.**
   서비스(`dcselfie.win`)는 live지만 사용자가 거의 없으므로 구조 변경이 가능하다.
   단, 각 migration stage는 별도의 review 가능한 PR이며 207개 전체 테스트를 green으로 유지해야 한다.
3. **새 dependency를 추가하지 않는다.**
   Python 3.12와 stdlib를 사용하고 `ruff`의 `E,F,W,I,B,UP`, line length 120을 준수한다.

## 4. 타겟 설계

### 4.1 책임 경계

| 모듈 | 책임 | 하지 않을 일 |
| --- | --- | --- |
| crawler | post 발견, source HTML 파싱, post 중복 조회 | 전송 성공 판정 |
| bot | source별 다운로드/처리 흐름, post ack 조정 | Discord payload 직접 조립 |
| `MediaPipeline` | fan-out 정책, 결과 집계, web enqueue 조정 | source crawling, SQLite 접근 |
| `MessageSender` | 한 destination에 대한 Discord/Telegram I/O와 즉시 fallback | post/hash ack 정책 |
| `DeliveryArchive` | 성공 확정된 identifier의 저장/조회 | destination별 retry 상태 저장 |

`launcher.py`와 bot constructor는 concrete dependency를 조립하는 바깥쪽 composition root로 유지한다.
정책은 `MediaPipeline` 안으로 모으고 Discord/FastAPI/SQLite 세부 구현은 경계 밖에 둔다.

### 4.2 Typed delivery result

새 stdlib dataclass는 `Module/delivery_result.py`에 두고 transport 구현과 분리한다.
구현 시 이름은 조정할 수 있지만 의미와 ack 계산식은 다음 계약을 지켜야 한다.

```python
class DeliveryOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    QUEUED = "queued"

@dataclass(frozen=True, slots=True)
class ChannelDelivery:
    transport: Literal["discord", "telegram", "web_gallery"]
    destination_id: str
    outcome: DeliveryOutcome
    requested_media: tuple[str, ...]
    delivered_media: tuple[str, ...]
    ack_eligible: bool
    reason: str | None = None

@dataclass(frozen=True, slots=True)
class DeliveryResult:
    deliveries: tuple[ChannelDelivery, ...]

    @property
    def acknowledged(self) -> bool:
        return any(
            item.ack_eligible and item.outcome is DeliveryOutcome.SUCCEEDED
            for item in self.deliveries
        )
```

- `reason`은 `channel_not_found`, `send_failed`, `queue_full` 같은 안정된 분류만 담고 exception 객체는 넘기지 않는다.
- batch fallback 일부만 성공하면 `PARTIAL`로 기록하고 `delivered_media`로 성공 hash를 식별한다.
- Discord와 DC의 Telegram만 `ack_eligible=True`다. Web gallery의 `QUEUED`는 ack에 포함하지 않는다.
- `DeliveryResult.__bool__`은 만들지 않는다. 호출자는 반드시 `result.acknowledged`를 사용해 정책을 드러낸다.
- 여러 image/batch 결과는 `DeliveryResult.merge()` 같은 순수 연산으로 합치며 mutable list나 중첩 tuple을 노출하지 않는다.

post ack 조건은 현재와 동일하게 유지한다.

- DC: `result.acknowledged`가 참이면 성공한 image hash와 post ID를 확정한다.
- Arca: `result.acknowledged and all_media_resolved`일 때만 post ID를 확정한다.
- 이미지가 없는 정상 post는 지금처럼 terminal success, detail/download의 일시 실패는 retry 대상으로 남긴다.
- 실패 destination의 다음 polling 독립 재시도는 추가하지 않는다. 기존 transport 내부 즉시 retry/413 fallback은 유지한다.

### 4.3 Discord fan-out 통합

`MediaPipeline.send_discord_batch(items, *, title, link, start_index=0) -> DeliveryResult`를
DC와 Arca의 유일한 Discord 진입점으로 만든다. DC는 길이 1인 batch를, Arca는 기존 batch를 넘긴다.

1. `_build_discord_payload()`가 `source_label`, `discord_embed_color`, global index를 사용해 files/embeds를 만든다.
2. 각 channel 전송 전 buffer를 rewind하고 payload를 새로 만든다.
3. 첫 embed만 title/link/footer를 가지며 footer 문구와 image count는 #112 동작을 그대로 유지한다.
4. `MessageSender`가 실제 `channel.send()`와 413의 개별 fallback/recompression을 담당한다.
5. `MediaPipeline`은 channel별 `ChannelDelivery`를 모으고 web enqueue 및 Telegram 결과를 merge한다.
6. `ArcaBot._send_fallback()`의 hash/web side effect는 제거하고 typed result를 받은 bot이 한 곳에서 확정한다.

통합 중 반드시 보존할 차이는 다음과 같다.

| 불변 동작 | 보존 방법 |
| --- | --- |
| DC Telegram fan-out | Discord 결과와 별도 outcome으로 merge |
| Arca Discord-only | `telegram_enabled=False` 유지 |
| DC web enqueue는 delivery ack와 무관 | `QUEUED/FAILED`, `ack_eligible=False`로 기록 |
| Arca web publish는 성공한 Discord batch 기준 | 성공 media만 enqueue |
| title/link는 첫 global image에만 표시 | `start_index`로 판단 |
| channel 하나 성공 시 ack 가능 | channel 결과에 `any` 적용 |
| 실패 channel 독립 retry 없음 | archive schema와 scheduling 변경 금지 |

caller migration이 끝나면 `send_single_to_channels()`와 `send_batch_to_channel()`은 제거한다.
deprecated wrapper를 새로 만들지 말고 Stage 2 한 PR 안에서 repository 내부 caller를 모두 전환한다.

### 4.4 명시적 domain/상태 타입

| 현재 표현 | 타겟 타입 | 적용 위치 |
| --- | --- | --- |
| media `dict[str, object]` | `PreparedMedia` dataclass (`BytesIO`, `str`, `bool`, `bytes`) | `media_pipeline.py`, 두 bot |
| `(item, resolved)` | `MediaPreparation` dataclass | `arca_bot.py::_download_and_process_one()` |
| post `object`/raw dict | source별 `TypedDict` 또는 dataclass | 두 crawler와 두 bot |
| process 2-tuple/3-tuple | `CrawlerProcessState` dataclass | `launcher.py` |
| sender 인자/반환 `object` | `BytesIO`, `discord.abc.Messageable`, `str`, `bool`/typed result | `message_sender.py` |
| embed helper `object` | `str | None`, `int`, `discord.Embed` | `embeds.py` |
| FastAPI handler `object` | `Request`, `Callable`, `Awaitable[Response]`, `Response` | `web_app.py` |
| intent/config 반환 미지정 | `discord.Intents`, `None` | `config.py` 및 entrypoint |

`PreparedMedia`에는 byte buffer와 hash만 담고 archive 또는 Discord 객체를 넣지 않는다.
`CrawlerProcessState`는 `process`, `started_at`, `restart_at`, `failures`를 명시해
`launcher.monitor_batch()`가 tuple 길이로 상태를 판별하지 않게 한다.

### 4.5 변경하지 않는 계약

- `delivery_archive` table, primary key, `post:`/`image:` namespace 및 `ARCHIVE_PATH`를 변경하지 않는다.
- `/`, `/feed`, `/images/{id}`, `/internal/images`, `/verify`, `/like/{id}`, `/healthz`의 method/status/body 계약을 변경하지 않는다.
- Discord/Telegram 메시지 수, 순서, embed title/link/footer, 413 retry 범위를 변경하지 않는다.
- RAM-only gallery 정책과 TTL/item/byte 제한을 변경하지 않는다.
- `Module/config.py`의 env 이름과 기본값, `galleries.json` 형식, launcher concurrency 기본값을 변경하지 않는다.

## 5. 마이그레이션 스테이지

모든 stage는 이전 stage가 merge된 최신 `origin/main`에서 시작하고 한 stage당 한 PR로 제출한다.
코드 stage는 focused test 후 `python3 -m pytest -q`의 207개 전체 테스트와 `python3 -m ruff check .`을 통과해야 한다.

### Stage 0 — 설계 고정 (DONE, 이 PR)

- 범위: 이 설계 문서 작성. 소규모 docs 정리가 필요하더라도 현재 PR은 `docs/refactoring-design.md` 한 파일로 제한한다.
- 파일: `docs/refactoring-design.md`.
- 위험: **낮음** — runtime 변경 없음.
- 검증: 문서의 경로/함수명, 확정 결정, stage 경계를 diff review한다. 이 작업 지시에 따라 local test는 실행하지 않는다.
- rollback: 파일을 되돌리면 되며 runtime/data rollback은 없다.

### Stage 1 — typed result 도입

- 범위: `ChannelDelivery`/`DeliveryResult`를 추가하고 기존 send 결과를 감싸되 실제 send 순서와 ack 판정은 유지한다.
- 파일: 새 `Module/delivery_result.py`, `Module/media_pipeline.py`, `Module/dcbot.py`, `Module/arca_bot.py`,
  `tests/test_media_pipeline.py`, `tests/test_dcbot.py`, `tests/test_arca_bot.py`.
- 위험: **낮음** — 기존 transport 호출은 유지하고 bot 경계에서만 `result.acknowledged`로 변환한다.
- focused 검증: channel A 실패/B 성공, Discord 실패/Telegram 성공, 모두 실패, web-only queued,
  Arca `all_media_resolved=False`, destination별 outcome 보존을 위 세 test module에 추가한다.
- 전체 검증: 207 tests + ruff. 실패 destination이 추가 호출되지 않는지도 mock call count로 고정한다.
- rollback: Stage 1 PR만 revert하면 기존 `bool` 집계로 복귀하며 archive migration은 없다.

### Stage 2 — DC/Arca Discord fan-out 통합

- 범위: 단일 `send_discord_batch()`와 payload builder로 양 source를 이동하고 Arca 413 fallback을 transport 경계로 옮긴다.
- 파일: `Module/media_pipeline.py`, `Module/message_sender.py`, `Module/dcbot.py`, `Module/arca_bot.py`, `Module/embeds.py`,
  `tests/test_media_pipeline.py`, `tests/test_message_sender.py`, `tests/test_dcbot.py`, `tests/test_arca_bot.py`.
- 위험: **중간** — message grouping, buffer 위치, fallback, web/hash side effect의 순서가 영향을 받을 수 있다.
- focused 검증: 단일/다중 batch parity, source footer, 첫 embed title/link, 다중 channel any-success,
  413 개별 fallback의 complete/partial failure, buffer rewind, 성공 media만 hash/web 확정하는 경우를 추가한다.
- 전체 검증: 207 tests + ruff. 기존 Discord/Telegram mock call shape도 characterization test로 비교한다.
- rollback: Stage 2 PR을 revert해 두 send 경로로 복귀한다. schema/env 변경이 없어 배포 rollback도 코드만 필요하다.

### Stage 3 — compatibility 제거와 annotation 구체화

- 범위: repository 및 운영 외부 script에 `ImageHandler.is_duplicate()` caller가 없음을 확인한 뒤 API와 해당 호환 테스트를 제거한다.
  이어서 `PreparedMedia`, post type, `CrawlerProcessState`를 도입하고 광범위한 `object` annotation을 치환한다.
- 파일: `Module/image_handler.py`, `Module/message_sender.py`, `Module/crawler.py`, `Module/arca_crawler.py`,
  `Module/dcbot.py`, `Module/arca_bot.py`, `Module/media_pipeline.py`, `Module/embeds.py`, `launcher.py`, `web_app.py`,
  `Module/config.py`와 대응하는 `tests/test_*.py`.
- 위험: **중간** — deprecated 외부 API 제거와 넓은 signature 변경이 포함된다.
- focused 검증: `tests/test_image_handler.py`, `tests/test_launcher.py`, `tests/test_crawler.py`,
  `tests/test_arca_crawler.py`, `tests/test_web_app.py` 및 Stage 1~2 delivery test를 실행한다.
- 전체 검증: 207 tests + ruff, repository-wide caller search, 실제 entrypoint import smoke를 확인한다.
- rollback: annotation/dataclass PR을 revert한다. 외부 caller가 뒤늦게 발견되면 한 release 동안 warning shim만 복구하고 제거를 재계획한다.

## 6. 제외/보류 항목

| 항목 | 이번 리팩터링에서 제외하는 이유 |
| --- | --- |
| per-destination acknowledgement | any-destination 유지가 확정됐으며 변경 시 archive key/schema와 중복 방지 정책이 달라진다. |
| archive retention window | 오래된 key 삭제는 과거 콘텐츠 재전송을 허용하므로 owner가 기간을 별도로 결정해야 한다. |
| `/verify` request-rate policy | threshold와 Cloudflare/origin 신뢰 모델, 공유 IP 영향에 대한 운영 결정이 먼저다. |
| live retention copy reconciliation | live override를 확인한 뒤 10분 안내 문구와 기본 3시간 설정 중 무엇을 바꿀지 정해야 한다. |

## 7. 참고

- [README](../README.md)
- 코드 품질 보고서: `/tmp/gallery-relay-quality-report.md` (작업 환경 로컬 산출물)
- [PR #111 — QR-001~005 품질 개선](https://github.com/mytrashcan/gallery-image-relay/pull/111)
- [PR #112 — `MediaPipeline.source_label`](https://github.com/mytrashcan/gallery-image-relay/pull/112)
