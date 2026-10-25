| File | Name | Manual recs | VLM recs | Matched | Missing | Extra | Field mismatches | Pipeline-identical |
|---|---|---|---|---|---|---|---|---|
| evidence_04 | account_admin_console_20260926.png | 2 | 2 | 2 | 0 | 0 | 2 | NO |
| evidence_09 | search_index_inspector_20260901.png | 2 | 2 | 2 | 0 | 0 | 4 | NO |
| evidence_14 | connector_management_console_20260807.png | - | - | - | - | - | - | no VLM result |
| evidence_19 | analytics_profile_view_20260713.png | - | - | - | - | - | - | no VLM result |
| evidence_23 | customer_export_console_20260618.png | - | - | - | - | - | - | no VLM result |
| evidence_28 | support_ticket_console_20260524.png | 1 | 1 | 1 | 0 | 0 | 1 | yes |
| evidence_32 | backup_restore_console_20260429.png | - | - | - | - | - | - | no VLM result |
| evidence_37 | security_audit_explorer_20260905.png | 6 | 6 | 6 | 0 | 0 | 6 | NO |
| evidence_42 | deleted_account_reference_20260310.png | - | - | - | - | - | - | no VLM result |
| evidence_46 | object_storage_browser_20260213.png | - | - | - | - | - | - | no VLM result |
| evidence_60 | marketing_profile_console.png | - | - | - | - | - | - | no VLM result |
| **total** | 4/11 files compared | 11 | 11 | 11 | 0 | 0 | 13 | 1/4 |

Differences:

- evidence_04 `account_5762` field `state`: manual=FOUND vs VLM=IDENTIFIABLE
- evidence_04 `support_9538` field `identifiers`: manual=['user_11'] vs VLM=[]
- evidence_09 `search_1874` field `state`: manual=FOUND vs VLM=IDENTIFIABLE
- evidence_09 `search_1874` field `observed_at`: manual=2026-09-01T05:55:03Z vs VLM=2026-09-01T06:00:14Z
- evidence_09 `search_428` field `state`: manual=FOUND vs VLM=IDENTIFIABLE
- evidence_09 `search_428` field `observed_at`: manual=2026-09-01T05:58:21Z vs VLM=2026-09-01T06:00:14Z
- evidence_28 `support_1034` field `observed_at`: manual=2026-05-24T07:00:00Z vs VLM=
- evidence_37 `audit_record_001` field `system_id`: manual=security_service vs VLM=Sentinel
- evidence_37 `audit_record_002` field `system_id`: manual=security_service vs VLM=Sentinel
- evidence_37 `audit_record_003` field `system_id`: manual=security_service vs VLM=Sentinel
- evidence_37 `audit_record_004` field `system_id`: manual=security_service vs VLM=Sentinel
- evidence_37 `audit_record_005` field `system_id`: manual=security_service vs VLM=Sentinel
- evidence_37 `audit_record_006` field `system_id`: manual=security_service vs VLM=Sentinel
