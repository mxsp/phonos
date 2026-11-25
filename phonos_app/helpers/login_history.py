"""login_history writes and the "known device" lookup, in one place.

ip_address and user_agent are personal data. With ``PHONOS_DATA_KEYS`` set they are stored sealed
(``data_crypto``) next to ``device_idx``, a keyed hash of ip + user agent that the new-device check
searches by (a sealed value cannot be compared in SQL). Without a key everything is exactly as before:
plaintext columns, ``device_idx`` stays NULL, the old query runs.
"""
from . import data_crypto as dc

L_IP = "login_history.ip_address"
L_UA = "login_history.user_agent"
L_DEV = "login_history.device"


def _device_text(ip_address, user_agent):
    return f"{ip_address or ''}\n{user_agent or ''}"


def record_login(db, user_id, ip_address, user_agent):
    """INSERT one login_history row; the caller commits."""
    if dc.enabled():
        db.execute(
            'INSERT INTO login_history (user_id, ip_address, user_agent, device_idx) VALUES (%s, %s, %s, %s)',
            (user_id, dc.seal(L_IP, ip_address), dc.seal(L_UA, user_agent),
             dc.blind_index(L_DEV, _device_text(ip_address, user_agent))))
    else:
        db.execute('INSERT INTO login_history (user_id, ip_address, user_agent) VALUES (%s, %s, %s)',
                   (user_id, ip_address, user_agent))


def is_known_device(db, user_id, ip_address, user_agent):
    if dc.enabled():
        # sealed rows by blind index (every ring key), rows from before encryption by plaintext
        indexes = dc.blind_indexes(L_DEV, _device_text(ip_address, user_agent))
        marks = ', '.join(['%s'] * len(indexes))
        return db.execute(
            'SELECT 1 FROM login_history WHERE user_id = %s AND '
            f'(device_idx IN ({marks}) OR (ip_address = %s AND user_agent = %s))',
            (user_id, *indexes, ip_address, user_agent)).fetchone()
    return db.execute('SELECT 1 FROM login_history WHERE user_id = %s AND ip_address = %s AND user_agent = %s',
                      (user_id, ip_address, user_agent)).fetchone()
