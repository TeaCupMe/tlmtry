# Windows Task Scheduler — run collector at startup
#
# 1. Install Python 3.11+ and the collector:
#      pip install -e collector/
# 2. Copy config\collector.example.yaml to e.g. C:\tlmtry\collector.yaml
#    Set port: COM3 (or your USB-RS485 port) and database path.
# 3. Create a task:
#      schtasks /Create /TN TlmtryCollector /SC ONSTART /RL HIGHEST ^
#        /TR "\"C:\Path\To\python.exe\" -m tlmtry_collector run --config C:\tlmtry\collector.yaml" ^
#        /RU SYSTEM
#
# Or use Task Scheduler GUI: Trigger = At startup, Action = start program
# python.exe with arguments: -m tlmtry_collector run --config C:\tlmtry\collector.yaml
#
# Alternative: NSSM / WinSW to wrap the same command as a Windows Service.
