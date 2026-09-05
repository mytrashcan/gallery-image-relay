"""메모리 전용 임시 웹 갤러리 서버.

launcher의 크롤러 프로세스는 localhost 내부 API로 이미지를 보내고, 이 프로세스가
bounded memory store를 단독 소유한다. 프로세스 종료 시 이미지도 모두 사라진다.
"""
import uvicorn

from Module.config import app_config
from web_app import create_app

if __name__ == "__main__":
    if not app_config.web_ingest_token:
        raise SystemExit("Set WEB_INGEST_TOKEN in the project .env before starting the server")
    # 기본값은 로컬 전용(127.0.0.1) — 외부 공개는 리버스 프록시(Caddy 등)를 통해서만.
    # 프록시 없이 직접 공개하려면 WEB_HOST=0.0.0.0 을 명시적으로 지정한다.
    uvicorn.run(create_app(), host=app_config.web_host, port=app_config.web_port, log_level="info")
