"""Nhóm lệnh prefix: logs.

Chỉnh danh sách COMMANDS trong file này để thêm/bớt tên lệnh thuộc nhóm logs.
Logic xử lý lệnh hiện nằm trong main.py, hàm run_prefix_action().
"""

COMMANDS = "modlog_set modlog_show modlog_test message_log_set message_log_toggle voice_log_set voice_log_toggle member_log_set member_log_toggle role_log_set role_log_toggle channel_log_set channel_log_toggle audit_recent mod_case mod_case_note mod_case_reason mod_case_delete mod_history mod_stats mod_top mod_note mod_notes mod_note_clear mod_report".split()
