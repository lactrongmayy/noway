"""Nhóm lệnh prefix: channel.

Chỉnh danh sách COMMANDS trong file này để thêm/bớt tên lệnh thuộc nhóm channel.
Logic xử lý lệnh hiện nằm trong main.py, hàm run_prefix_action().
"""

COMMANDS = "lock unlock slowmode slowmode_off channel_create channel_delete channel_rename channel_topic channel_topic_clear channel_clone channel_move category_create category_delete category_rename channel_nsfw channel_safe channel_position channel_sync channel_unsync channel_permissions channel_allow channel_deny channel_reset channel_pin channel_unpin".split()
