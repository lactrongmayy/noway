"""Nhóm lệnh prefix: moderation.

Chỉnh danh sách COMMANDS trong file này để thêm/bớt tên lệnh thuộc nhóm moderation.
Logic xử lý lệnh hiện nằm trong main.py, hàm run_prefix_action().
"""

COMMANDS = "kick ban unban softban timeout untimeout warn warnings clearwarnings warn_remove purge purge_user purge_bots purge_links purge_images purge_files purge_mentions purge_contains nickname_set nickname_reset role_add role_remove role_list member_quarantine member_release".split()
