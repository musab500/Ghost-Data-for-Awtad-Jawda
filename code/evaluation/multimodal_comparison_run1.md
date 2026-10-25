| File | Name | Manual recs | VLM recs | Matched | Missing | Extra | Field mismatches | Pipeline-identical |
|---|---|---|---|---|---|---|---|---|
| evidence_04 | account_admin_console_20260926.png | 2 | 2 | 2 | 0 | 0 | 4 | NO |
| evidence_09 | search_index_inspector_20260901.png | 2 | 2 | 2 | 0 | 0 | 4 | NO |
| evidence_14 | connector_management_console_20260807.png | 2 | 2 | 2 | 0 | 0 | 2 | yes |
| evidence_19 | analytics_profile_view_20260713.png | 1 | 1 | 1 | 0 | 0 | 0 | yes |
| evidence_23 | customer_export_console_20260618.png | 4 | 4 | 4 | 0 | 0 | 5 | NO |
| evidence_28 | support_ticket_console_20260524.png | - | - | - | - | - | - | no VLM result |
| evidence_32 | backup_restore_console_20260429.png | 4 | 4 | 4 | 0 | 0 | 8 | NO |
| evidence_37 | security_audit_explorer_20260905.png | 6 | 6 | 6 | 0 | 0 | 6 | NO |
| evidence_42 | deleted_account_reference_20260310.png | 1 | 1 | 1 | 0 | 0 | 2 | NO |
| evidence_46 | object_storage_browser_20260213.png | 1 | 1 | 1 | 0 | 0 | 1 | yes |
| evidence_60 | marketing_profile_console.png | 1 | 1 | 1 | 0 | 0 | 1 | NO |
| **total** | 10/11 files compared | 24 | 24 | 24 | 0 | 0 | 33 | 3/10 |

Differences:

- evidence_04 `account_5762` field `system_id`: manual=account_service vs VLM=identity_service
- evidence_04 `account_5762` field `state`: manual=FOUND vs VLM=IDENTIFIABLE
- evidence_04 `support_9538` field `state`: manual=FOUND vs VLM=IDENTIFIABLE
- evidence_04 `support_9538` field `identifiers`: manual=['user_11'] vs VLM=['+819076102507', 'haruka.s1838@zoho.com', 'user_11']
- evidence_09 `search_1874` field `state`: manual=FOUND vs VLM=IDENTIFIABLE
- evidence_09 `search_1874` field `observed_at`: manual=2026-09-01T05:55:03Z vs VLM=2026-09-01T06:00:14Z
- evidence_09 `search_428` field `state`: manual=FOUND vs VLM=IDENTIFIABLE
- evidence_09 `search_428` field `observed_at`: manual=2026-09-01T05:58:21Z vs VLM=2026-09-01T06:00:14Z
- evidence_14 `data_flow_1121` field `observed_at`: manual=2026-08-07T07:59:42Z vs VLM=2026-08-07T08:00:00Z
- evidence_14 `data_flow_9846` field `observed_at`: manual=2026-08-07T07:59:36Z vs VLM=2026-08-07T08:00:00Z
- evidence_23 `export_1258` field `state`: manual=FOUND vs VLM=IDENTIFIABLE
- evidence_23 `export_1259` field `state`: manual=FOUND vs VLM=IDENTIFIABLE
- evidence_23 `export_1260` field `state`: manual=FOUND vs VLM=IDENTIFIABLE
- evidence_23 `export_1260` field `observed_at`: manual=2026-06-18T06:46:00Z vs VLM=
- evidence_23 `export_1261` field `state`: manual=FOUND vs VLM=IDENTIFIABLE
- evidence_32 `backup_9286` field `state`: manual=ACTIVE vs VLM=IDENTIFIABLE
- evidence_32 `backup_9286` field `observed_at`: manual= vs VLM=2026-04-26T02:00:00Z
- evidence_32 `backup_9287` field `state`: manual=ACTIVE vs VLM=IDENTIFIABLE
- evidence_32 `backup_9287` field `observed_at`: manual= vs VLM=2026-04-26T02:00:00Z
- evidence_32 `backup_9288` field `state`: manual=ACTIVE vs VLM=IDENTIFIABLE
- evidence_32 `backup_9288` field `observed_at`: manual= vs VLM=2026-04-26T02:00:00Z
- evidence_32 `backup_9291` field `state`: manual=ACTIVE vs VLM=IDENTIFIABLE
- evidence_32 `backup_9291` field `observed_at`: manual= vs VLM=2026-04-26T02:00:00Z
- evidence_37 `audit_record_001` field `state`: manual=FOUND vs VLM=IDENTIFIABLE
- evidence_37 `audit_record_002` field `state`: manual=FOUND vs VLM=IDENTIFIABLE
- evidence_37 `audit_record_003` field `state`: manual=FOUND vs VLM=IDENTIFIABLE
- evidence_37 `audit_record_004` field `state`: manual=FOUND vs VLM=IDENTIFIABLE
- evidence_37 `audit_record_005` field `state`: manual=FOUND vs VLM=IDENTIFIABLE
- evidence_37 `audit_record_006` field `state`: manual=FOUND vs VLM=IDENTIFIABLE
- evidence_42 `tombstone_17` field `category`: manual=ACCOUNT_TOMBSTONE vs VLM=USER_PROFILE
- evidence_42 `tombstone_17` field `state`: manual=REDACTED vs VLM=RETAINED
- evidence_46 `object_19` field `observed_at`: manual=2026-02-12T18:42:00Z vs VLM=2026-02-13T08:00:00Z
- evidence_46 manual record dropped by validation: no resource id or alias in ['delivery_confirmation_20.pdf']
- evidence_46 manual record dropped by validation: no resource id or alias in ['return_label_4821.pdf']
- evidence_46 manual record dropped by validation: no resource id or alias in ['return_label_4876.pdf']
- evidence_46 manual record dropped by validation: no resource id or alias in ['invoice_NS-10493.pdf']
- evidence_46 manual record dropped by validation: no resource id or alias in ['parcel_damage_7713.jpg']
- evidence_46 manual record dropped by validation: no resource id or alias in ['customer_export_2026-02-10.zip']
- evidence_46 manual record dropped by validation: no resource id or alias in ['customer_export_2026-02-12.zip']
- evidence_46 VLM record dropped by validation: no resource id or alias in ['delivery_confirmation_20.pdf']
- evidence_46 VLM record dropped by validation: no resource id or alias in ['return_label_4821.pdf']
- evidence_46 VLM record dropped by validation: no resource id or alias in ['return_label_4876.pdf']
- evidence_46 VLM record dropped by validation: no resource id or alias in ['invoice_NS-10493.pdf']
- evidence_46 VLM record dropped by validation: no resource id or alias in ['parcel_damage_7713.jpg']
- evidence_46 VLM record dropped by validation: no resource id or alias in ['customer_export_2026-02-10.zip']
- evidence_46 VLM record dropped by validation: no resource id or alias in ['customer_export_2026-02-12.zip']
- evidence_60 `marketing_001` field `state`: manual=FOUND vs VLM=IDENTIFIABLE
