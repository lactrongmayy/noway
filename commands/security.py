"""Nhóm lệnh prefix: security.

Chỉnh danh sách COMMANDS trong file này để thêm/bớt tên lệnh thuộc nhóm security.
Logic xử lý lệnh hiện nằm trong main.py, hàm run_prefix_action().
"""

COMMANDS = "antispam_on antispam_off antispam_limit antispam_window antispam_action antiinvite_on antiinvite_off antilink_on antilink_off antilink_allow antilink_deny badword_add badword_remove badword_list badword_toggle caps_limit mention_limit duplicate_limit emoji_limit new_account_limit raid_mode_on raid_mode_off raid_threshold raid_status security_report".split()
