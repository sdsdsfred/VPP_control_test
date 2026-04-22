import sys
import time
import socketio

sio = socketio.Client(logger=True, engineio_logger=True)

@sio.event
def connect():
    print('[test_client] connected')

@sio.event
def disconnect():
    print('[test_client] disconnected')

@sio.on('update')
def on_update(data):
    try:
        h = data.get('hour')
        print(f'[test_client] received update hour={h} keys={list(data.keys())[:6]}')
    except Exception as e:
        print('[test_client] received update (could not parse):', e)

if __name__ == '__main__':
    try:
        sio.connect('http://127.0.0.1:8002')
        # wait for a few messages or user ctrl-c
        for _ in range(60):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print('connect failed:', e)
    finally:
        try:
            sio.disconnect()
        except Exception:
            pass
        print('client exiting')
