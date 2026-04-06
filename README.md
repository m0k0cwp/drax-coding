# DRAX Coding — Associate Performance Automation

Automates DRAX associate performance row editing for Stationary Picking.

## Script: `drax_single_associate.py`

Searches for an associate by **first name** (or full name), filters their timeline rows for a given date/shift, and submits the correct Modified SC Code for all eligible idle events.

### Skip Rules
| Condition | Action |
|---|---|
| Green flag in Exemption | Skip — enters exempt window |
| Inside exempt window | Skip |
| Checkered flag | Skip — exits exempt window |
| No Idle pause icon | Skip |
| Dept ≠ Stationary Picking | Skip |
| Time 13:50 – 14:50 | Skip (lunch block) |
| User column not empty | Skip (already edited) |
| No edit link | Skip |

### Configuration (top of script)
```python
TARGET_ASSOCIATE_NAME = "TEKINA"       # First name or full name
DATE                  = "2026-04-06"   # Date to process
SHIFT                 = "S1"
MODIFIED_SC           = "Downtime Systems (900080911)"
TARGET_DEPT           = "Stationary Picking"
SKIP_TIME_START       = "13:50:00"
SKIP_TIME_END         = "14:50:00"
```

### Usage
```bash
python drax_single_associate.py
```

> Requires Walmart VPN or Eagle WiFi. Browser session is persisted in `drax_browser_profile/`.
