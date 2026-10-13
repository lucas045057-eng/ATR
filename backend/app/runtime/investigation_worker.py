"""Investigation loop with no database, model configuration or SSH credentials.

Every advance is interpreted by the API broker against its frozen authorized
state. This process cannot submit tool arguments or an authoritative conclusion.
"""
import json
import sys
import threading


def main():
    initial = json.loads(sys.stdin.buffer.readline(65536))
    if set(initial) != {'run_id', 'lease_token'}:
        raise ValueError('Invalid investigation initialization')
    lock, stop = threading.Lock(), threading.Event()
    sequence = 0

    def emit(kind, payload):
        nonlocal sequence
        with lock:
            sequence += 1
            message = {**initial, 'sequence': sequence, 'type': kind, 'payload': payload}
            sys.stdout.write(json.dumps(message) + '\n')
            sys.stdout.flush()

    def heartbeat():
        while not stop.wait(1):
            emit('heartbeat', {})

    threading.Thread(target=heartbeat, daemon=True).start()
    try:
        while True:
            emit('progress', {'operation': 'advance'})
            line = sys.stdin.buffer.readline(2 * 1024 * 1024 + 1)
            if not line or len(line) > 2 * 1024 * 1024:
                raise ValueError('Missing or oversized broker response')
            response = json.loads(line)
            if set(response) != {'continue'} or type(response['continue']) is not bool:
                raise ValueError('Invalid broker response')
            if not response['continue']:
                break
    finally:
        stop.set()


if __name__ == '__main__':
    main()
