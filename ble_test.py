#!/usr/bin/env python3
"""BLE color test — try multiple chars and protocols."""
import asyncio
from bleak import BleakClient

ADDRESS  = "14F98C93-E17A-2484-2BDD-76C2CE5C9C4A"
CHAR_FF01 = "0000ff01-0000-1000-8000-00805f9b34fb"
CHAR_FF11 = "0000ff11-0000-1000-8000-00805f9b34fb"


async def send(client, char, data, label, use_response=False):
    try:
        await client.write_gatt_char(char, data, response=use_response)
        print(f"  OK  [{label}] -> {data.hex()}")
    except Exception as e:
        print(f"  ERR [{label}] {e}")


async def main():
    print(f"Connecting to {ADDRESS}...")
    async with BleakClient(ADDRESS, timeout=10.0) as client:
        print(f"Connected: {client.is_connected}")

        # power on (common for many generic controllers)
        await send(client, CHAR_FF01, bytes([0xcc, 0x23, 0x33]), "power-on ff01")
        await send(client, CHAR_FF11, bytes([0xcc, 0x23, 0x33]), "power-on ff11")
        await asyncio.sleep(1)

        # RED via ff01 (7e protocol)
        await send(client, CHAR_FF01, bytes([0x7e, 0x07, 0x05, 0x03, 0xff, 0x00, 0x00, 0x00, 0xef]), "RED ff01 7e")
        await asyncio.sleep(1.5)

        # RED via ff11 (7e protocol)
        await send(client, CHAR_FF11, bytes([0x7e, 0x07, 0x05, 0x03, 0xff, 0x00, 0x00, 0x00, 0xef]), "RED ff11 7e")
        await asyncio.sleep(1.5)

        # GREEN via ff11
        await send(client, CHAR_FF11, bytes([0x7e, 0x07, 0x05, 0x03, 0x00, 0xff, 0x00, 0x00, 0xef]), "GREEN ff11 7e")
        await asyncio.sleep(1.5)

        # Triones protocol on ff11
        await send(client, CHAR_FF11, bytes([0x56, 0xff, 0x00, 0x00, 0x00, 0xf0, 0xaa]), "RED ff11 56")
        await asyncio.sleep(1.5)

        # try with response=True on ff01
        await send(client, CHAR_FF01, bytes([0x7e, 0x07, 0x05, 0x03, 0x00, 0x00, 0xff, 0x00, 0xef]), "BLUE ff01 7e resp", use_response=True)
        await asyncio.sleep(1.5)

        print("Done — which (if any) fired?")

asyncio.run(main())
