"""VOD Recorder(코드 이름 live-recorder): 치지직·유튜브·씨미 실시간 녹화와 다시보기·영상 받기 도구.

내부 식별자(묶음 `live_recorder`, `LIVE_RECORDER_` 환경변수, 폴더·DB·로거·API·설정 키)는 코드 이름을 따른다.
"""

__version__ = "0.1.0"
# 프로그램의 표시 이름을 정하는 한 자리. 화면·알림·컨테이너 로그 안내·로그 묶음·진단 요약이 이 값을 쓴다
PROGRAM_NAME = "VOD Recorder"
# 화면 문서(HTML)에서 표시 이름이 들어갈 자리. 서버가 내려 줄 때 PROGRAM_NAME으로 바꾼다
NAME_SLOT = "{{PROGRAM_NAME}}"
