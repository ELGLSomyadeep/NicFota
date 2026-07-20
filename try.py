"""
DLMS/COSEM relay control using the real gurux-dlms library
============================================================

Unlike a raw byte replay, this builds a fresh, correctly-signed SNRM /
AARQ / HLS-authentication handshake for every run, using the gurux_dlms
library to do the actual DLMS/COSEM protocol work (framing, sequence
numbers, segmentation, and the GMAC authentication response). That's
what makes it work session after session instead of only replaying one
frozen capture.

Install:
    pip install gurux-dlms gurux-common gurux-serial pyserial

Usage:
    python dlms_relay_gurux.py status
    python dlms_relay_gurux.py disconnect
    python dlms_relay_gurux.py connect

============================================================
BEFORE RUNNING: fill in the CONFIG block below
============================================================
Everything in CONFIG has to match your Gurux Director project for this
meter. Gurux does not print these values in the communication trace
(the log you get from "View -> Trace"), so they need to come from the
project itself:

  - In Director: open the device, then Properties -> Media (HDLC) for
    the client/server address, and Properties -> Association for the
    Authentication level, Password, and (for HLS/GMAC) System title,
    Authentication key, Block cipher key.
  - Or open the saved project file (.gxp) in a text editor - it's XML,
    and the same values appear under tags like <Authentication>,
    <Password>, <SystemTitle>, <AuthenticationKey>, <BlockCipherKey>.

If AUTHENTICATION is Authentication.HIGH_GMAC (the most likely case
based on your capture - the AARQ carries a fresh random challenge every
session), SYSTEM_TITLE / AUTHENTICATION_KEY / BLOCK_CIPHER_KEY are
required - the handshake will fail without the real values.
"""
"""
DLMS/COSEM relay control using the real gurux-dlms library
============================================================

Unlike a raw byte replay, this builds a fresh, correctly-signed SNRM /
AARQ / HLS-authentication handshake for every run, using the gurux_dlms
library to do the actual DLMS/COSEM protocol work (framing, sequence
numbers, segmentation, and the GMAC authentication response). That's
what makes it work session after session instead of only replaying one
frozen capture.

Install:
    pip install gurux-dlms gurux-common gurux-serial pyserial

Usage:
    python dlms_relay_gurux.py status
    python dlms_relay_gurux.py disconnect
    python dlms_relay_gurux.py connect

============================================================
BEFORE RUNNING: fill in the CONFIG block below
============================================================
Everything in CONFIG has to match your Gurux Director project for this
meter. Gurux does not print these values in the communication trace
(the log you get from "View -> Trace"), so they need to come from the
project itself:

  - In Director: open the device, then Properties -> Media (HDLC) for
    the client/server address, and Properties -> Association for the
    Authentication level, Password, and (for HLS/GMAC) System title,
    Authentication key, Block cipher key.
  - Or open the saved project file (.gxp) in a text editor - it's XML,
    and the same values appear under tags like <Authentication>,
    <Password>, <SystemTitle>, <AuthenticationKey>, <BlockCipherKey>.

If AUTHENTICATION is Authentication.HIGH_GMAC (the most likely case
based on your capture - the AARQ carries a fresh random challenge every
session), SYSTEM_TITLE / AUTHENTICATION_KEY / BLOCK_CIPHER_KEY are
required - the handshake will fail without the real values.
"""

import sys
import time
import argparse

from gurux_dlms import GXDLMSClient, GXReplyData, GXByteBuffer, GXDLMSException
from gurux_dlms.enums import Authentication, InterfaceType, Security
from gurux_dlms.objects import GXDLMSDisconnectControl
from gurux_dlms.secure.GXDLMSSecureClient import GXDLMSSecureClient

try:
    import serial
except ImportError:
    print("pyserial is required. Install it with: pip install pyserial")
    sys.exit(1)


# ============================================================
# CONFIG - fill these in from your Gurux Director project
# ============================================================

PORT = "COM7"
BAUD_RATE = 9600

# HDLC logical addresses (NOT the raw byte seen in the trace - Gurux
# encodes these as addr*2+1 onto the wire). Director: Properties -> Media.
# Defaults below (server=1, client=126) reproduce the 0x03 / 0xFD bytes
# seen in your latest working capture - print SNRM_HEX at startup and
# compare it to your trace to confirm, adjust if it doesn't match.
SERVER_ADDRESS = 1
CLIENT_ADDRESS = 126

# Director: Properties -> Association -> Authentication.
# One of: Authentication.NONE, .LOW, .HIGH, .HIGH_MD5, .HIGH_SHA1,
#         .HIGH_GMAC, .HIGH_SHA256, .HIGH_ECDSA
AUTHENTICATION = Authentication.HIGH_GMAC

# Only used when AUTHENTICATION == Authentication.LOW (not used at HIGH_GMAC -
# a "MR mode" password belongs here only if you switch AUTHENTICATION to
# Authentication.LOW; it is a different value from SYSTEM_TITLE below and is
# not read at all while AUTHENTICATION stays HIGH_GMAC).
PASSWORD = b""

# Only used for HIGH_GMAC / other HLS levels - Director: Properties ->
# Association -> Security tab, or <SystemTitle>/<AuthenticationKey>/
# <BlockCipherKey> in the .gxp file.
#
# SYSTEM_TITLE must be exactly 8 bytes - it is NOT the password and NOT the
# auth/block cipher key. The value below ("ESYA0000") was found decoded
# directly out of your own AARQ capture in GXDLMSDirector.txt (the bytes
# 45 53 59 41 30 30 30 30 = ASCII "ESYA0000" as the calling-AP-title) - it's
# a strong guess for this device, but verify it against Director's Security
# settings if authentication still fails after this fix.
SYSTEM_TITLE = b"ESYA0000"                                              # 8 bytes
AUTHENTICATION_KEY = bytes.fromhex("000102030405060708090A0B0C0D0E0F")  # 16 bytes
BLOCK_CIPHER_KEY = bytes.fromhex("000102030405060708090A0B0C0D0E0F")    # 16 bytes

# Data exchange (GET/ACTION requests) was plaintext in your capture, so
# ciphering security stays NONE - only the authentication step uses the
# keys above. Change this only if your meter also encrypts data.
DATA_SECURITY = Security.NONE

OBIS_DISCONNECT_CONTROL = "0.0.96.3.10.255"

TIMEOUT_SECONDS = 5.0


# ============================================================
# Low-level transport: feed raw serial bytes into the DLMS client
# ============================================================

def read_dlms_packet(ser: serial.Serial, client: GXDLMSClient, data, reply: GXReplyData):
    """
    Send one or more raw frames and read until the client reports a
    complete DLMS reply. client.getData() understands HDLC framing
    internally, so we just keep feeding it bytes as they arrive.
    """
    if not data:
        return
    frames = data if isinstance(data, list) else [data]
    for frame in frames:
        reply.clear()
        rd = GXByteBuffer()
        notify = GXReplyData()
        ser.reset_input_buffer()
        ser.write(frame)
        ser.flush()

        deadline = time.time() + TIMEOUT_SECONDS
        while not client.getData(rd, reply, notify):
            if time.time() > deadline:
                raise TimeoutError("No reply from meter (timed out).")
            waiting = ser.in_waiting
            if waiting:
                rd.set(ser.read(waiting))
            else:
                time.sleep(0.01)


def read_data_block(ser: serial.Serial, client: GXDLMSClient, data, reply: GXReplyData):
    """
    Like read_dlms_packet, but also handles multi-block replies (where
    the meter needs a "receiver ready" ack before sending the rest).
    """
    if not data:
        return
    if isinstance(data, list):
        for item in data:
            reply.clear()
            read_data_block(ser, client, item, reply)
        return
    read_dlms_packet(ser, client, data, reply)
    while reply.isMoreData():
        next_request = client.receiverReady(reply)
        read_dlms_packet(ser, client, next_request, reply)


# ============================================================
# High level operations
# ============================================================

def build_client() -> GXDLMSClient:
    # GXDLMSSecureClient (a subclass of GXDLMSClient) is required whenever
    # HLS/GMAC authentication is used - it's the one that exposes
    # .ciphering (system title / auth key / block cipher key / security).
    client = GXDLMSSecureClient(
        useLogicalNameReferencing=True,
        clientAddress=CLIENT_ADDRESS,
        serverAddress=SERVER_ADDRESS,
        forAuthentication=AUTHENTICATION,
        interfaceType=InterfaceType.HDLC,
    )
    if AUTHENTICATION == Authentication.LOW:
        client.password = PASSWORD
    else:
        client.ciphering.systemTitle = SYSTEM_TITLE
        client.ciphering.authenticationKey = AUTHENTICATION_KEY
        client.ciphering.blockCipherKey = BLOCK_CIPHER_KEY
        client.ciphering.security = DATA_SECURITY
    return client


def connect_session(ser: serial.Serial, client: GXDLMSClient):
    reply = GXReplyData()

    data = client.snrmRequest()
    print("TX (SNRM):", GXByteBuffer.hex(data))
    read_dlms_packet(ser, client, data, reply)
    client.parseUAResponse(reply.data)
    print("SNRM/UA ok.")

    reply.clear()
    read_data_block(ser, client, client.aarqRequest(), reply)
    client.parseAareResponse(reply.data)
    print("AARQ/AARE ok.")

    if client.authentication > Authentication.LOW:
        reply.clear()
        for frame in client.getApplicationAssociationRequest():
            read_dlms_packet(ser, client, frame, reply)
        client.parseApplicationAssociationResponse(reply.data)
        print("HLS authentication ok.")


def read_status(ser: serial.Serial, client: GXDLMSClient):
    dc = GXDLMSDisconnectControl(OBIS_DISCONNECT_CONTROL)
    reply = GXReplyData()

    data = client.read(dc, 2)[0]  # output state
    read_data_block(ser, client, data, reply)
    client.updateValue(dc, 2, reply.value)
    print("Output state:", dc.outputState)

    reply.clear()
    data = client.read(dc, 3)[0]  # control state
    read_data_block(ser, client, data, reply)
    client.updateValue(dc, 3, reply.value)
    print("Control state:", dc.controlState)


def disconnect_relay(ser: serial.Serial, client: GXDLMSClient):
    dc = GXDLMSDisconnectControl(OBIS_DISCONNECT_CONTROL)
    reply = GXReplyData()
    read_data_block(ser, client, dc.remoteDisconnect(client), reply)
    print("Relay disconnect command sent.")


def connect_relay(ser: serial.Serial, client: GXDLMSClient):
    dc = GXDLMSDisconnectControl(OBIS_DISCONNECT_CONTROL)
    reply = GXReplyData()
    read_data_block(ser, client, dc.remoteReconnect(client), reply)
    print("Relay reconnect command sent.")


def release_and_disconnect(ser: serial.Serial, client: GXDLMSClient):
    try:
        reply = GXReplyData()
        read_data_block(ser, client, client.releaseRequest(), reply)
    except Exception:
        pass  # not all meters support RLRQ
    try:
        reply = GXReplyData()
        read_dlms_packet(ser, client, client.disconnectRequest(), reply)
    except Exception:
        pass


# ============================================================
# Entry point
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="DLMS relay control via gurux-dlms")
    parser.add_argument("action", choices=["status", "disconnect", "connect"])
    parser.add_argument("--port", default=PORT)
    parser.add_argument("--baud", type=int, default=BAUD_RATE)
    args = parser.parse_args()

    print(f"Opening {args.port} @ {args.baud} baud...")
    ser = serial.Serial(
        port=args.port,
        baudrate=args.baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=1,
    )

    client = build_client()

    try:
        connect_session(ser, client)

        if args.action == "status":
            read_status(ser, client)
        elif args.action == "disconnect":
            disconnect_relay(ser, client)
        elif args.action == "connect":
            connect_relay(ser, client)

        release_and_disconnect(ser, client)

    except GXDLMSException as e:
        print(f"DLMS error: {e}")
        sys.exit(1)
    except TimeoutError as e:
        print(f"Timeout: {e}")
        print("Check CLIENT_ADDRESS/SERVER_ADDRESS and the security settings in CONFIG.")
        sys.exit(1)
    finally:
        ser.close()
        print("Serial port closed.")


if __name__ == "__main__":
    main()