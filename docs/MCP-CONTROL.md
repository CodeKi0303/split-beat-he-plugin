# 키보드 MCP · LLM 제어

Split-Beat HE Studio에서 하는 설정을 로컬 MCP 도구로 제어할 수 있습니다. 일반 키·마우스 버튼·스크롤·미디어 매핑, 여러 Hall 키의 작동점/RT/데드존, 캘리브레이션, 프로필, 센서 진단과 앱 펌웨어 업데이트를 제공합니다. 도구를 등록하거나 서버를 시작하는 것만으로 실제 키보드에 연결하거나 설정을 바꾸지 않습니다.

서버는 키보드가 연결된 PC에서 실행합니다. **PC USB는 왼쪽에 연결하고 오른쪽은 기존 좌우 케이블을 통해 제어**합니다. 오른쪽을 PC에 따로 연결할 필요가 없습니다. 외부 서버·터널·계정은 필요하지 않으며 이 구성은 인터넷에서 키보드를 조작하는 원격 서비스가 아닙니다.

## 설치와 연결

저장소 루트에서 Python 환경을 준비합니다. 이미 `.venv`가 있으면 생성 명령은 생략합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
```

Codex용 설정은 [codex-keyboard.toml](../examples/codex-keyboard.toml)입니다. 필요한 테이블만 사용자 `~/.codex/config.toml` 또는 프로젝트 `.codex/config.toml`에 병합합니다. 다른 위치로 복사했다면 Python과 `host/run_mcp.py`의 절대 경로를 모두 변경합니다. 등록 후 MCP 서버를 다시 시작하고 `codex mcp list`로 확인할 수 있습니다. STDIO는 로컬 실행 명령과 인자를 등록하는 방식입니다. [Codex 공식 MCP 설정 문서](https://developers.openai.com/codex/mcp).

다른 MCP 클라이언트는 다음 JSON 중 하나를 해당 앱의 MCP 설정에 병합합니다. `mcpServers` 포맷 지원 여부는 클라이언트 설명을 확인합니다.

| 예제 | 허용 범위 |
|---|---|
| [mcp-hardware-readonly.json](../examples/mcp-hardware-readonly.json) | 장치 탐색·명시적 연결·설정 읽기·진단 |
| [mcp-hardware-control.json](../examples/mcp-hardware-control.json) | 위 기능 + 보정·매핑·설정·저장·출력 제어 |
| [mcp-firmware-control.json](../examples/mcp-firmware-control.json) | 위 기능 + 명시적으로 요청한 앱 펌웨어 업데이트 |
| [mcp-demo.json](../examples/mcp-demo.json) | 실제 USB를 열지 않는 시뮬레이션 |

모든 예제의 경로는 예시 Windows 절대 경로입니다. 다른 OS에서는 Python 경로와 저장소 경로를 그 환경의 절대 경로로 바꿉니다. 서버는 `cwd`가 없어도 실행할 수 있는 `host/run_mcp.py`를 사용합니다.

실제 장치는 `keyboard_list_devices`로 찾고 반환된 정확한 serial을 `keyboard_connect`에 전달합니다. `--serial`을 지정한 서버도 그 값을 기억할 뿐 자동으로 USB를 열지 않습니다. `--demo`는 합성 장치에만 연결합니다. `keyboard_session`으로 연결/권한 상태를 확인하고, `keyboard_disconnect`로 USB 핸들을 해제합니다. 연결 종료는 설정이나 출력 상태를 바꾸지 않습니다.

브라우저 설정창이 같은 장치와 통신 중이면 그 창에서 **연결 종료** 후 MCP를 연결합니다. MCP에서 끝난 뒤 연결 종료하면 다시 WebHID를 사용할 수 있습니다.

## 지원 도구

MCP 클라이언트가 도구 이름 앞에 서버 이름을 붙일 수 있습니다. 인자와 범위는 실행 중 도구 스키마가 기준입니다.

| 작업 | 도구 |
|---|---|
| 장치·연결 | `keyboard_list_devices`, `keyboard_connect`, `keyboard_disconnect`, `keyboard_session` |
| 정보·상태·배치 | `keyboard_info`, `keyboard_status`, `keyboard_layout`, `keyboard_health` |
| 설정 읽기 | `keyboard_get_config`, `keyboard_export_profile`, `keyboard_validate_profile` |
| 여러 Hall 키 설정 | `keyboard_set_travel_mm`, `keyboard_preview_keys`, `keyboard_set_keys` |
| 입력 매핑 | `keyboard_binding_catalog`, `keyboard_set_bindings`, `keyboard_set_binding` |
| 개별 보정·설정 | `keyboard_calibrate`, `keyboard_set_key` |
| 전체/선택 키 보정 | `keyboard_batch_calibration_start`, `keyboard_batch_calibration_status`, `keyboard_batch_calibration_apply`, `keyboard_batch_calibration_cancel` |
| 프로필·기본값 | `keyboard_export_studio_profile`, `keyboard_import_studio_profile`, `keyboard_import_profile`, `keyboard_defaults` |
| 저장·실제 출력 | `keyboard_save`, `keyboard_output` |
| 센서 통계 | `keyboard_capture_sensors` |
| 눌림·해제 시계열 | `keyboard_capture_key_timing` |
| RT 자동 맞춤 | `keyboard_autofit_begin`, `keyboard_autofit_status`, `keyboard_autofit_capture`, `keyboard_autofit_recommend`, `keyboard_autofit_apply`, `keyboard_autofit_cancel` |
| 자동 맞춤 기록 | `keyboard_autofit_export`, `keyboard_autofit_analyze` |
| 다른 키 영향 비교 | `keyboard_interference_baseline`, `keyboard_interference_compare` |
| 장치 성능·통신 | `keyboard_measure_performance`, `keyboard_measure_link` |
| 앱 업데이트 | `keyboard_firmware_inspect`, `keyboard_firmware_update` |
| 시뮬레이션 포즈 | `keyboard_demo_pose` — 데모 전용 |

키 위치는 `keyboard_layout`에서 확인합니다. 입력은 Hall 14개, 일반 접점·클릭 6개, 인코더 방향 8개입니다. 작동점·RT·데드존은 Hall 키에만 적용합니다. 키보드 usage, 조합키, 마우스 5버튼, 세로/가로 스크롤, 지원 미디어 키는 카탈로그로 찾을 수 있습니다. 임의 문자열 매크로 실행이나 운영체제 키 입력 기록 도구는 제공하지 않습니다.

## 설정이 적용되는 범위

자동 맞춤은 보정된 Hall 키 하나와 확인된 스위치 총 스트로크를 사용합니다. `keyboard_autofit_begin`에 키, `total_travel_mm`, 사용 목적 `balanced`·`repeat`·`hold`를 전달합니다. 도구가 알려 주는 자세를 사용자에게 안내하고 준비됐다는 응답을 받은 뒤 `keyboard_autofit_capture`를 호출합니다. 순서는 `released` 3초, `resting` 3초, `tapping` 10초, `holding` 5초입니다. 반복 입력은 편한 속도로 6회 이상 충분히 눌렀다 놓습니다. 길게 누르기는 사용자가 이미 누른 상태에서 측정을 시작합니다.

네 단계가 끝나면 `keyboard_autofit_recommend`가 관찰한 입력을 여러 작동점·RT 설정으로 재생해 후보를 비교합니다. 사용자가 자동 설정을 요청한 범위에서 `keyboard_autofit_apply`를 호출하면 측정한 키에 적용하고 전체 설정을 읽어 대조합니다. `keyboard_autofit_begin`의 `retest=true`로 같은 키·스트로크·사용 목적을 다시 측정하면 실제 눌림 판정과 첫 측정 결과를 함께 확인할 수 있습니다. 새로 시작하거나 중단하려면 `keyboard_autofit_cancel`을 호출합니다. 진행 중에는 다른 설정 작업을 잠시 잠급니다.

추천은 호스트가 관찰한 센서 샘플을 사용합니다. 매우 짧은 변화는 샘플 사이에서 누락될 수 있으며 mm는 선택한 보정 곡선과 스트로크로 추정합니다. MCP의 반복 입력은 자기 속도로 진행하며 소리와 비교하려면 Studio의 리듬 측정을 사용합니다. 기록을 `keyboard_autofit_export`로 내보내고 Studio의 같은 형식 기록을 `keyboard_autofit_analyze`로 분석할 수 있습니다. 저장과 키 출력은 사용자의 요청에 따라 `keyboard_save`와 `keyboard_output`으로 마무리합니다.

| 동작 | 결과 |
|---|---|
| 미리보기 | 변경할 값과 환산을 확인하며 장치 설정은 변경하지 않음 |
| 키/매핑/보정 적용 | RAM에 적용하고 읽어 확인. 일반 설정 변경 시 출력은 꺼짐 |
| `keyboard_save` | Flash 저장 후 같은 장치에 재연결해 저장 세대와 전체 설정을 대조 |
| `keyboard_output(enabled=true)` | 실제 PC 입력 켜기. 미보정/오래된 센서 등 조건이 맞지 않으면 장치가 거부 |
| Studio 프로필 | 장치 설정과 키별 총 스트로크 메타데이터를 함께 내보내거나 가져옴 |

단체 설정은 선택한 키의 지정한 항목만 바꾸며 기존 보정과 나머지 설정을 유지합니다. 미리보기의 fingerprint를 적용 요청에 전달하면 그 사이 설정이 바뀐 경우 적용을 중단합니다. 사용자 요청에 “저장하고 입력 켜줘”가 있으면 그 단계까지 수행할 수 있습니다. 설정만 요청했다면 저장·출력을 임의로 추가하지 않습니다. 실패한 변경은 자동으로 Flash에 저장하지 않고 현재 값을 다시 읽어 확인합니다. `keyboard_defaults`는 보정과 매핑까지 초기화하므로 단순 설정 변경에 사용할 필요가 없습니다.

mm는 **스위치의 총 스트로크**를 기준으로 정규화한 값입니다. 키캡 높이가 아닙니다. 세션의 기본 스트로크 값은 실측 정보가 아니므로, 사용자 값 또는 기존 Studio 프로필을 확인합니다. 스트로크는 장치 Flash 필드가 아니라 세션/Studio 프로필 메타데이터입니다. 거리 설정은 해당 키의 유효한 보정이 먼저 필요합니다. `noise_deadband`는 mm 모드에서도 원시 ADC 카운트입니다. 0.01 mm 설정 간격은 두 점 보정의 물리적 정확도를 보장하지 않습니다.

일반 프로필의 `c77-profile-v1`이라는 형식 이름은 C77/C85/C86이 공유하는 데이터 스키마 이름입니다. 이 이름으로 실제 보드 리비전을 판단하지 않습니다. 현재 버전·지원 기능은 `keyboard_info`에서 확인합니다. 비영(非零) 하단 데드존은 capability `0x100`이 있는 펌웨어가 필요하고, 준비한 업데이트 파일만으로 기능이 활성화되지는 않습니다.

## 전체 캘리브레이션과 진단

모든 키에서 손을 뗀 상태에서 보정을 시작하면 3초간 기준을 수집합니다. 이후 선택한 Hall 키를 각각 끝까지 누릅니다. 상태 도구가 수집·적용 가능한 키를 보여 주며 유효한 키만 부분 적용할 수 있습니다. 잡음만으로 눌림 완료를 간주하거나 미수집 키를 덮어쓰지 않습니다. 보정 중 실제 키 자세는 사용자가 만들어야 합니다.

센서 통계는 최대 10초, 초당 5–50회의 호스트 관찰을 사용합니다. ADC 전체 파형이나 자기 간섭의 직접 측정이 아닙니다. 다른 키 영향 비교는 전부 놓은 기준과 한 키만 누른 상태를 비교합니다. 장치 성능 도구는 ADC/USB/링크의 펌웨어 카운터를 읽으며 Windows 수신율의 별도 USB 추적 검증과 구분합니다. 데모 결과는 실물 검증 결과가 아닙니다.

`keyboard_capture_key_timing`은 선택 Hall 키 하나를 최대 10초·초당 5–100회 관찰합니다. ADC·이동량·실제 눌림 상태와 요청/응답 시각, 센서 데이터 나이, 눌림·해제 시점의 관찰 구간 및 유지 시간을 반환합니다. 설정과 출력 상태는 바꾸지 않습니다. 통신 공백이나 리셋을 가로질러 이벤트를 추정하지 않으며, `timing_usable=false`이면 RT 추천에 사용하지 않습니다.

소리와 비교하려면 WebHID 화면의 **리듬 · RT 측정**을 사용합니다. BPM과 한 마디의 8·12·16분할 등을 지정하고 메트로놈 반복 입력 또는 피아노 유지·해제를 시험합니다. 그래프에서 소리 기준, 보정한 목표, 누름 깊이와 실제 판정을 함께 확인합니다. 목표 보정은 양수면 소리보다 늦게, 음수면 먼저입니다. 반복 입력으로 제안하는 개인 오프셋은 손 타이밍·오디오·장치 지연을 합친 관찰값이며 키보드만의 지연이 아닙니다. 작동점·복귀 RT 후보는 관찰이 충분할 때만 보여 주고 자동 적용하지 않습니다.

## 앱 펌웨어 업데이트

`--firmware-write-enabled`는 업데이트 도구를 허용할 뿐 자동으로 기록하지 않습니다. 실제 호출에는 사용자가 요청한 패키지/대상, 검사 결과의 **`image_sha256`**을 전달한 `expected_sha256`과 `confirm_update=true`가 필요합니다. 패키지 파일 자체의 해시인 `package_file_sha256`과 구분합니다. 일반 설정 허용인 `--write-enabled`만으로는 펌웨어를 기록할 수 없습니다.

먼저 `keyboard_firmware_inspect`로 로컬 `.fw.json` 패키지의 내용·해시·하드웨어/좌우 대상을 확인합니다. 실제 업데이트는 호환 영구 부팅부가 설치된 C86에서 왼쪽 USB를 통해 왼쪽 또는 오른쪽 앱에 수행합니다. 설치 여부는 장치 사전 검사로 확인하며, 준비된 파일/앱 capability만으로 단정하지 않습니다.

업데이트는 출력과 스캔을 일시 중지하고 재시작합니다. 저장한 설정 영역을 보존하며, **미저장 RAM 변경이나 진행 중 보정이 있으면 MCP 업데이트를 시작하지 않습니다.** 필요한 설정을 먼저 내보내고 요청에 따라 저장하거나 보정을 종료합니다. 업데이트 도구가 자동 저장하지는 않습니다. 전체 앱 읽기 대조를 기본으로 유지합니다. 영구 부팅부 자체 갱신, 임의 Flash/레지스터 쓰기, 자동 롤백은 제공하지 않습니다. 자세한 설치·복구는 [FIRMWARE-UPDATE.md](https://github.com/CodeKi0303/split-beat-he-firmware/blob/main/docs/FIRMWARE-UPDATE.md)를 따릅니다.

## 자연어로 요청하기

- “현재 연결된 키보드의 버전과 오른쪽 연결 상태만 확인해줘.”
- “WASD의 총 스트로크는 4 mm야. 작동점 0.8 mm, 눌림·복귀 RT 0.1 mm로 맞추고 저장한 뒤 입력 켜줘.”
- “오른쪽 휠을 위/아래 스크롤로, 왼쪽 휠을 볼륨으로 바꿔줘. 아직 저장하지 마.”
- “모든 Hall 키를 한 번에 보정하고 싶어. 내가 눌러야 하는 순서 안내해줘.”
- “손을 뗀 상태의 센서 떨림을 5초 측정하고 가장 흔들리는 키를 알려줘.”
- “선택한 Hall 키를 5초간 누르고 떼는 상태를 측정해서 유지 시간과 해제 간격을 보여줘.”
- “현재 보정과 매핑, 총 스트로크를 Studio 프로필로 내보내줘.”
- “이 펌웨어 패키지를 검사만 하고 어느 쪽용인지 알려줘.”

재사용 스킬은 [skills/keyboard-control](../plugins/split-beat-he-plugin/skills/keyboard-control/SKILL.md)에 있습니다. Codex에서 설치한 경우 `$keyboard-control`로 호출하거나 키보드 설정 요청에 자동으로 적용할 수 있습니다. 스킬은 도구의 사용 순서와 물리 보정 절차를 안내하며, MCP 서버 자체를 대신하지 않습니다.


## 미해제 재입력 허용

`keyboard_preview_keys` / `keyboard_set_keys`의 patch에
`{"retrigger_before_release": true}`를 지정하거나 `keyboard_set_key`의 같은
boolean 인자를 사용합니다. 기본은 꺼짐이며 capability `0x40000`이 필요합니다.
RT가 켜진 키가 해제 거리까지 올라오지 않았어도, 올라온 최저점에서 다시
`rt_press`만큼 누르면 실제 USB 해제→눌림을 한 번 보냅니다. 계속 누르는
것만으로 반복 입력하지 않습니다. 일반 RT 해제와 상·하단 데드존이 우선합니다.
이 옵션은 롱노트를 끊을 수 있습니다. RT를 끄면 옵션은 저장된 채 동작하지
않습니다. 지정하지 않은 필드와 보정·매핑은 유지됩니다.

전체 프로필에서는 key.flags의 `0x08` 비트로 보관하며, 기존 22바이트
키 레코드 크기는 유지합니다. `keyboard_key_options`는 키별 `retrigger_before_release`와 실제 활성 조건을
이름으로 보여 주며, 미리보기에는 `proposed_options`로 나타납니다.
`keyboard_get_config` / 프로필 내보내기는 원래 flags 레코드를 유지합니다. 지원하지 않는 펌웨어에는 프로필·개별·단체 설정
모두 장치 변경 전에 거부합니다. False로 끄는 것은 기존 펌웨어에서도 됩니다.

호스트 STATUS의 pressed 값은 논리적인 유지 상태이므로 USB의 짧은
해제→눌림 펄스를 보여주지 않습니다. 자동 맞춤은 옵션이 켜진 키를 거부하고,
눌림·해제 시간 분석은 RT와 옵션이 함께 켜졌을 때 거부합니다. 원시 센서
진단은 계속 사용할 수 있습니다. 데모와 SampledRT 모델은 재입력 요청의
발생만 모사하며 USB 전송 완료나 실제 게임의 입력을 검증하지 않습니다.
