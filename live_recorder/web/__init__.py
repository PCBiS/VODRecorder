"""웹 서버. 뼈대는 `app.py`(앱 만들기·접근 관문·정적 파일·공통 오류 형식)·`auth.py`(인증)·`access.py`(접근 규칙),
기능별 경로는 `routes/`의 묶음이다. 아래는 이 패키지가 밖에 내놓는 이름이다."""

from .access import ACCESS_RULES, LOGIN, PUBLIC, VIEW, access_level
from .app import UI_DIR, UIStaticFiles, content_security_policy, create_app, run
from .auth import SESSION_COOKIE, VIEW_ONLY_GUIDE

__all__ = ["ACCESS_RULES", "LOGIN", "PUBLIC", "SESSION_COOKIE", "UIStaticFiles", "UI_DIR", "VIEW", "VIEW_ONLY_GUIDE",
           "access_level", "content_security_policy", "create_app", "run"]
