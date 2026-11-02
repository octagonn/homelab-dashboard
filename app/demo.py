"""Publication helper: synthetic Portainer responses; no network calls."""
_round = 0
def payload(path):
    global _round
    if path == '/api/endpoints':
        _round += 1
        return [{'Id': i, 'Name': f'demo-node-{i}', 'URL': f'tcp://node-{i}.example.invalid:9001', 'Status': 1} for i in [1, 2]]
    if path == '/api/stacks':
        return [{'Id': 1, 'EndpointId': 1, 'Name': 'example-stack', 'Status': 1, 'Type': 2}]
    if '/docker/containers/json' in path:
        node = int(path.split('/')[3])
        active = 1 if _round % 2 else 2
        if node != active: return []
        return [{'Id': 'synthetic-container', 'Names': ['/demo-inference'], 'Image': 'example/inference:demo', 'State': 'running', 'Status': 'Synthetic example', 'Ports': []}]
    return None
