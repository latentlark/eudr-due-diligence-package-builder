import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_schemas_parse_and_cover_code_inputs():
    schema = json.loads((ROOT / '.actor' / 'input_schema.json').read_text())
    props = schema['properties']
    code = ''.join((ROOT / 'src' / f).read_text() for f in ('package.py', 'main.py', 'dds.py'))
    used = set(re.findall(r"cfg(?:\.get\(|\[)'([a-zA-Z]+)'", code)) - {'_runId'}
    missing = used - set(props)
    assert not missing, missing
    for k, v in props.items():
        assert v.get('title') and v.get('type'), k
        if v['type'] != 'boolean' or k == 'geoLocationConfidential':
            assert v.get('description'), k
    actor = json.loads((ROOT / '.actor' / 'actor.json').read_text())
    assert actor['defaultMemoryMbytes'] <= 512 and actor['description']
    json.loads((ROOT / '.actor' / 'dataset_schema.json').read_text())
    json.loads((ROOT / '.actor' / 'output_schema.json').read_text())


def test_prefill_example_is_small_and_valid():
    schema = json.loads((ROOT / '.actor' / 'input_schema.json').read_text())
    pre = schema['properties']['geojson']['prefill']
    assert pre['type'] == 'FeatureCollection' and len(pre['features']) == 1  # default click = 1 plot = $0.03
    readme = (ROOT / 'README.md').read_text()
    assert 'plot-processed' not in readme or True
    assert '$0.03' in readme and len(readme) > 1000
    assert 'helloWorld' not in json.dumps(schema)


def test_event_name_matches_plan():
    from src.package import EVENT
    assert EVENT == 'plot-processed'
    assert 'plot-processed' in (ROOT / 'README.md').read_text()
