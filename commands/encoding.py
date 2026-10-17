"""Nhóm lệnh prefix: encoding.

Chỉnh danh sách COMMANDS trong file này để thêm/bớt tên lệnh thuộc nhóm encoding.
Logic xử lý lệnh hiện nằm trong main.py, hàm run_prefix_action().
"""

COMMANDS = "base64_encode base64_decode url_encode url_decode html_escape html_unescape json_format json_minify json_validate uuid hash_sha256 hash_sha512 hash_md5 password_generate password_strength random_hex random_string random_uuid binary_encode binary_decode hex_encode hex_decode morse_encode morse_decode qr_text".split()
