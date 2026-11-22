import json
from pathlib import Path
import unittest


class RecordsTest(unittest.TestCase):
    def test_known_input_contract(self):
        source = Path(__file__).resolve().parents[1] / "data" / "records.json"
        payload = json.loads(source.read_text(encoding="utf-8"))
        frame = {item['segment']: item for item in payload['frame']}
        drawn = {item['unit'] for item in payload['draws']}
        assert all(item['unit'] in drawn for item in payload['results'])
        for item in payload['draws']:
            assert item['unit'] in frame
            if item['mode'] == '预先随机':
                assert 0 < item['n'] < item['N']
        assert all(item['canonical'] in frame for item in payload['revisions'] if item['kind'] == '重复申报')
        assert any(item['unit'] not in frame for item in payload['revisions'] if item['kind'] == '后补')
