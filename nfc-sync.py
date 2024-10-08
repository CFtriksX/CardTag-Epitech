#!/usr/bin/env python3
import abc
import argparse
import cv2
import getpass
import nfc
import os
import requests
import time
from typing import Any

def timeout(start: time.time, seconds: int = 2):
    def elapsed_fx():
        return time.time() - start > seconds

    return elapsed_fx

class AbstractUserFinder(metaclass=abc.ABCMeta):
    @abc.abstractmethod
    def find_user(self, card_id: str) -> str | None:
        return None

class ETokenUserFinder(AbstractUserFinder):
    def find_user(self, card_id: str) -> str | None:
        return requests.get(f'https://whatsupdoc.epitech.eu/card/{card_id}').json().get('login', None)

class QRCodeUserFinder(AbstractUserFinder):
    def __init__(self, camera_path: int | str = 0):
        self.__camera  = cv2.VideoCapture(camera_path)
        self.__decoder = cv2.QRCodeDetector()

    def __decode(self, frame) -> str | None:
        data, bounds, straightened = self.__decoder.detectAndDecode(frame)
        color = (0, 255, 0) if data != '' else (0, 0, 255)
        if bounds is not None:
            # FIXME: This does not seem to work...
            count = len(bounds[0])
            for i in range(count):
                point1 = tuple(map(round, bounds[0][i]))
                point2 = tuple(map(round, bounds[0][(i + 1) % count]))
                frame = cv2.line(frame, point1, point2, color=color, thickness=2)

        cv2.imshow('frame', frame)
        return data if data != '' else None

    def find_user(self, card_id: str) -> str | None:
        while True:
            attempt = input('\x1b[1mAttempt to read card QR code?\x1b[0m [Y/n] ').lower()
            if attempt in ('n', 'no'):
                return None
            elif attempt in ('', 'y', 'yes'):
                break
        x = 0;
        while True:
            ok, frame = self.__camera.read()
            if x != 5:
                x += 1;
                continue
            if not ok:
                print('\x1b[31mError performing video capture\x1b[0m')
                return None
            data = self.__decode(frame)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                cv2.destroyAllWindows()
                return None
            if data is not None:
                cv2.destroyAllWindows()
                return data

class ManualUserFinder(AbstractUserFinder):
    def find_user(self, card_id: str) -> str | None:
        print('\x1b[1mUser could not automatically be determined\x1b[0m')
        user = input('User email to associate to the card: ')
        if user == '':
            return None
        return user

class CardAPI:
    def __init__(self, token: str):
        self.__token = token

    @staticmethod
    def from_credentials(username: str, password: str) -> 'CardAPI':
        res = requests.post('https://console.bocal.org/auth/login', json={
            'id': username,
            'password': password
        })

        if res.status_code != requests.codes.ok:
            raise ValueError(res.json().get('message', 'Unknown HTTP error'))
        print('Login successful')
        return CardAPI(res.json()['token'])

    def assign_card(self, username: str, card_id: str):
        fragments = username.split('@')
        if len(fragments) != 2:
            raise ValueError('Invalid email')

        res = requests.post(
            f'https://console.bocal.org/api/{fragments[1]}/users/{username}/card',
            headers={'Authorization': f'Bearer {self.__token}'},
            json={'id': card_id},
        )

        if res.status_code == requests.codes.forbidden:
            print('Card already assigned to someone else!')
        elif res.status_code != requests.codes.ok:
            raise ValueError(res.json())


class TagReader:
    __RDWR_OPTIONS: dict[str, Any] = {
        'beep-on-connect': True,
        'on-connect':      lambda tag: False,
    }

    def __init__(self, path: str = 'usb', timeout: int = 2):
        self.__path = path
        self.__timeout = timeout

    def wait_for_tag(self) -> str | None:
        with nfc.ContactlessFrontend(self.__path) as device:
            try:
                tag = device.connect(
                    rdwr=self.__RDWR_OPTIONS,
                    terminate=timeout(time.time(), self.__timeout),
                )
                if tag is False:
                    raise KeyboardInterrupt
                if tag is not None:
                    return tag.identifier.hex()
            except Exception as e:
                print(f'\x1b[31mNFC read failed: {e}\x1b[0m')
                # SMELL: Sleep in case of NFC failure to avoid being stuck in a 100% CPU loop
                time.sleep(self.__timeout)

            return None

class CardSynchronizer:
    def __init__(self, cards: CardAPI, finders: list[AbstractUserFinder], reader_path: str = 'usb', deduplication: bool = False):
        self.__cards         = cards
        self.__reader        = TagReader(reader_path)
        self.__finders       = finders
        self.__pairings      = {}
        self.__deduplication = deduplication

    def run(self):
        card_id = self.__reader.wait_for_tag()
        if card_id is None:
            return

        user = None
        for finder in self.__finders:
            user = finder.find_user(card_id)
            if user is not None:
                break

        if user is not None:
            if self.__pairings.get(card_id, None) == user and self.__deduplication:
                print(f'\x1b[37m{card_id} -> {user} already sent, not resending...\x1b[0m')
            else:
                print(f'{card_id} -> {user}')
                # FIXME: File name should be modifiable
                with open('output.csv', 'a') as f:
                    f.write(f'{user},{card_id}\n')
                self.__pairings[card_id] = user
                self.__cards.assign_card(user, card_id)
        else:
            print(f'{card_id} could not be matched to any user, skipping...')

def main():
    parser = argparse.ArgumentParser(description='Register student cards, fast and cheap')
    parser.add_argument('-E', '--no-etoken', dest='etoken', action='store_false', help='Disable requesting EToken for student identities')
    parser.add_argument('-Q', '--no-qr', dest='qr', action='store_false', help='Disable attempting to read student identities from QR codes')
    parser.add_argument('-M', '--no-manual', dest='manual', action='store_false', help='Prevent from prompting the user for student identities')
    parser.add_argument('-D', '--no-deduplication', dest='deduplication', action='store_false', help='Prevent request deduplication')
    parser.add_argument('-c', '--camera', metavar='PATH', default=0, help='Specify camera to read QR codes')
    parser.add_argument('-r', '--reader', metavar='PATH', default='usb', help='Specify NFC reader to read cards')
    config = parser.parse_args()

    if 'BOCAL_TOKEN' in os.environ:
        cards = CardAPI(os.environ['BOCAL_TOKEN'])
    else:
        print('\x1b[1mPlease log in to the Bocal API\x1b[0m')
        cards = CardAPI.from_credentials(input('Username: '), getpass.getpass())

    finders = []
    if config.etoken:
        finders.append(ETokenUserFinder())
    if config.qr:
        finders.append(QRCodeUserFinder(config.camera))
    if config.manual:
        finders.append(ManualUserFinder())

    if len(finders) == 0:
        print('No finders enabled, cannot operate...')
    
    sync = CardSynchronizer(cards, finders, config.reader, config.deduplication)
    while True:
        sync.run()

if __name__ == '__main__':
    try:
        main()
    except EOFError:
        print('\x1b[2K\x1b[G\x1b[1;37mExiting...\x1b[0m')
    except KeyboardInterrupt:
        print('\x1b[2K\x1b[G\x1b[1;37mExiting...\x1b[0m')
