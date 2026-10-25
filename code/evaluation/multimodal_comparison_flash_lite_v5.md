| File | Name | Manual recs | VLM recs | Matched | Missing | Extra | Field mismatches | Pipeline-identical |
|---|---|---|---|---|---|---|---|---|
| evidence_04 | account_admin_console_20260926.png | 2 | 2 | 2 | 0 | 0 | 2 | NO |
| evidence_09 | search_index_inspector_20260901.png | 2 | 2 | 2 | 0 | 0 | 5 | NO |
| evidence_14 | connector_management_console_20260807.png | 2 | 2 | 2 | 0 | 0 | 2 | yes |
| evidence_19 | analytics_profile_view_20260713.png | 1 | 1 | 1 | 0 | 0 | 1 | yes |
| evidence_23 | customer_export_console_20260618.png | 4 | 4 | 4 | 0 | 0 | 8 | NO |
| evidence_28 | support_ticket_console_20260524.png | 1 | 1 | 1 | 0 | 0 | 2 | NO |
| evidence_32 | backup_restore_console_20260429.png | 4 | 5 | 4 | 0 | 1 | 0 | NO |
| evidence_37 | security_audit_explorer_20260905.png | 6 | 6 | 6 | 0 | 0 | 5 | yes |
| evidence_42 | deleted_account_reference_20260310.png | 1 | 1 | 1 | 0 | 0 | 3 | NO |
| evidence_46 | object_storage_browser_20260213.png | 1 | 1 | 1 | 0 | 0 | 1 | yes |
| evidence_60 | marketing_profile_console.png | 1 | 1 | 0 | 1 | 1 | 0 | NO |
| **total** | 11/11 files compared | 25 | 26 | 24 | 1 | 2 | 29 | 4/11 |

Differences:

- evidence_04 `account_5762` field `state`: manual=FOUND vs VLM=IDENTIFIABLE
- evidence_04 `support_9538` field `identifiers`: manual=['user_11'] vs VLM=[]
- evidence_04 VLM record dropped by validation: no resource id or alias in ['ORD-20265762']
- evidence_04 VLM record dropped by validation: no resource id or alias in ['ORD-20265518']
- evidence_09 `search_1874` field `state`: manual=FOUND vs VLM=IDENTIFIABLE
- evidence_09 `search_1874` field `observed_at`: manual=2026-09-01T05:55:03Z vs VLM=2026-09-01T06:00:14Z
- evidence_09 `search_428` field `state`: manual=FOUND vs VLM=IDENTIFIABLE
- evidence_09 `search_428` field `identifiers`: manual=['g.silva76@gmail.com', 'user_13'] vs VLM=['g.silva76@gmail.com']
- evidence_09 `search_428` field `observed_at`: manual=2026-09-01T05:58:21Z vs VLM=2026-09-01T06:00:14Z
- evidence_14 `data_flow_1121` field `observed_at`: manual=2026-08-07T07:59:42Z vs VLM=2026-08-07T08:00:00Z
- evidence_14 `data_flow_9846` field `observed_at`: manual=2026-08-07T07:59:36Z vs VLM=2026-08-07T08:00:00Z
- evidence_19 `analytics_7438` field `observed_at`: manual=2026-07-13T14:22:00Z vs VLM=
- evidence_23 `export_1258` field `identifiers`: manual=['daniel.park@northstar.shop'] vs VLM=[]
- evidence_23 `export_1258` field `observed_at`: manual= vs VLM=2026-06-16T09:27:00Z
- evidence_23 `export_1259` field `identifiers`: manual=['sara.kim@northstar.shop'] vs VLM=[]
- evidence_23 `export_1259` field `observed_at`: manual= vs VLM=2026-06-17T11:03:00Z
- evidence_23 `export_1260` field `state`: manual=FOUND vs VLM=IDENTIFIABLE
- evidence_23 `export_1260` field `observed_at`: manual=2026-06-18T06:46:00Z vs VLM=2026-06-18T06:42:00Z
- evidence_23 `export_1261` field `identifiers`: manual=['marcus.lee@northstar.shop'] vs VLM=[]
- evidence_23 `export_1261` field `observed_at`: manual= vs VLM=2026-06-20T14:18:00Z
- evidence_28 `support_1034` field `category`: manual=SUPPORT_MESSAGE vs VLM=USER_PROFILE
- evidence_28 `support_1034` field `observed_at`: manual=2026-05-24T07:00:00Z vs VLM=
- evidence_28 VLM record dropped by validation: no resource id or alias in ['SWF-884103']
- evidence_32 **extra** in VLM output: `backup_9281`
- evidence_37 `audit_record_001` field `observed_at`: manual=2026-08-24T16:14:00Z vs VLM=
- evidence_37 `audit_record_002` field `observed_at`: manual=2026-08-25T03:21:00Z vs VLM=
- evidence_37 `audit_record_004` field `observed_at`: manual=2026-08-18T14:05:00Z vs VLM=
- evidence_37 `audit_record_005` field `observed_at`: manual=2026-08-20T09:42:00Z vs VLM=
- evidence_37 `audit_record_006` field `observed_at`: manual=2026-08-22T11:27:00Z vs VLM=
- evidence_42 `tombstone_17` field `state`: manual=REDACTED vs VLM=ACTIVE
- evidence_42 `tombstone_17` field `identifiers`: manual=['user_17'] vs VLM=[]
- evidence_42 `tombstone_17` field `observed_at`: manual=2026-03-10T07:18:00Z vs VLM=22 Aug 2024 15:03
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
- evidence_60 **missing** in VLM output: `marketing_001`
- evidence_60 **extra** in VLM output: `marketing_1`
