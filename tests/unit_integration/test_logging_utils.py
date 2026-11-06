import io
import json
import logging

from fraud_common.logging_utils import JsonFormatter


def test_vietnamese_log_survives_windows_cp1252_pipe():
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding='cp1252')
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter('worker'))
    record = logging.LogRecord('test', logging.WARNING, __file__, 1,
                               'Giao dịch lỗi: %s', ('chưa xác minh',), None)
    record.ctx = {'note': 'Kiểm tra số tiền'}
    handler.emit(record)
    stream.flush()
    data = json.loads(raw.getvalue().decode('cp1252'))
    assert data['msg'] == 'Giao dịch lỗi: chưa xác minh'
    assert data['note'] == 'Kiểm tra số tiền'


def test_exception_text_remains_readable_after_json_decoding():
    try:
        raise ValueError('Dữ liệu không hợp lệ')
    except ValueError:
        import sys
        record = logging.LogRecord('test', logging.ERROR, __file__, 1,
                                   'Xử lý thất bại', (), sys.exc_info())
    encoded = JsonFormatter('worker').format(record)
    encoded.encode('cp1252')
    data = json.loads(encoded)
    assert data['msg'] == 'Xử lý thất bại'
    assert 'ValueError: Dữ liệu không hợp lệ' in data['exc']
