#!/usr/bin/env python

import sys
import struct
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QTextEdit, QLineEdit,
                             QLabel, QComboBox, QMessageBox)
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtBluetooth import (QBluetoothDeviceDiscoveryAgent, QBluetoothSocket,
                               QBluetoothAddress, QBluetoothUuid, QBluetoothServiceInfo)


class EV3Protocol:
    """EV3 Protocol message formatting"""

    # Command types
    DIRECT_COMMAND_REPLY = 0x00
    DIRECT_COMMAND_NO_REPLY = 0x80
    SYSTEM_COMMAND_REPLY = 0x01
    SYSTEM_COMMAND_NO_REPLY = 0x81

    # Reply types
    DIRECT_REPLY = 0x02
    SYSTEM_REPLY = 0x03
    DIRECT_REPLY_ERROR = 0x04
    SYSTEM_REPLY_ERROR = 0x05

    # System commands
    BEGIN_DOWNLOAD = 0x92
    CONTINUE_DOWNLOAD = 0x93
    BEGIN_UPLOAD = 0x94
    CONTINUE_UPLOAD = 0x95
    LIST_FILES = 0x99
    CREATE_DIR = 0x9B
    DELETE_FILE = 0x9C
    WRITEMAILBOX = 0x9E

    # Opcodes for direct commands
    opSOUND = 0x94
    opUI_DRAW = 0x84
    opOUTPUT_STEP_SPEED = 0xAE
    opINPUT_DEVICE = 0x99

    # Sub-commands
    TONE = 0x01
    FILLWINDOW = 0x13
    UPDATE = 0x00
    READY_SI = 0x1D

    def __init__(self):
        self.msg_counter = 0

    @staticmethod
    def encode_lc0(value):
        """Encode short constant (single byte, +/- 31)"""
        if -31 <= value <= 31:
            return bytes([value & 0x3F])
        raise ValueError("Value out of range for LC0")

    @staticmethod
    def encode_lc1(value):
        """Encode long constant (one byte to follow, +/- 127)"""
        return bytes([0x81, value & 0xFF])

    @staticmethod
    def encode_lc2(value):
        """Encode long constant (two bytes to follow, +/- 32767)"""
        return bytes([0x82]) + struct.pack('<h', value)

    @staticmethod
    def encode_lc4(value):
        """Encode long constant (four bytes to follow)"""
        return bytes([0x83]) + struct.pack('<i', value)

    @staticmethod
    def encode_lcs(string):
        """Encode zero-terminated string"""
        return bytes([0x84]) + string.encode('utf-8') + b'\x00'

    @staticmethod
    def encode_gv0(index):
        """Encode global variable index (single byte)"""
        return bytes([0x60 | (index & 0x1F)])

    def build_message(self, cmd_type, payload, global_vars=0, local_vars=0):
        """Build complete EV3 message with header"""
        # Header: global and local variable allocation
        header = struct.pack('<H', (local_vars << 10) | global_vars)

        # Message body
        body = bytes([cmd_type]) + header + payload

        # Message counter (2 bytes, little endian)
        counter = struct.pack('<H', self.msg_counter)
        self.msg_counter = (self.msg_counter + 1) & 0xFFFF

        # Complete message with length prefix
        msg_length = len(body) + 2  # +2 for counter
        length_prefix = struct.pack('<H', msg_length)

        return length_prefix + counter + body

    def play_tone(self, volume, frequency, duration, reply=False):
        """Create a play tone direct command"""
        cmd_type = self.DIRECT_COMMAND_REPLY if reply else self.DIRECT_COMMAND_NO_REPLY

        payload = (bytes([self.opSOUND, self.TONE]) +
                  self.encode_lc1(volume) +
                  self.encode_lc2(frequency) +
                  self.encode_lc2(duration))

        return self.build_message(cmd_type, payload)

    def read_sensor(self, port, mode=0):
        """Create a read sensor direct command (returns 4 bytes float)"""
        payload = (bytes([self.opINPUT_DEVICE, self.READY_SI]) +
                  self.encode_lc0(0) +  # Layer 0
                  self.encode_lc0(port) +  # Sensor port (0-3)
                  self.encode_lc0(0) +  # Don't change type
                  self.encode_lc0(mode) +  # Mode
                  self.encode_lc0(1) +  # One dataset
                  self.encode_gv0(0))  # Store in global var 0

        return self.build_message(self.DIRECT_COMMAND_REPLY, payload, global_vars=4)

    def stop_motor(self, motor_bits, brake=True):
        """Stop motors (motor_bits: 1=A, 2=B, 4=C, 8=D)"""
        payload = (bytes([0xA3, 0x00]) +  # opOUTPUT_STOP, layer 0
                  self.encode_lc0(motor_bits) +
                  self.encode_lc0(1 if brake else 0))

        return self.build_message(self.DIRECT_COMMAND_NO_REPLY, payload)

    def start_motor(self, motor_bits, speed):
        """Start motors at speed (motor_bits: 1=A, 2=B, 4=C, 8=D, speed: -100 to 100)"""
        payload = (bytes([0xA5, 0x00]) +  # opOUPUT_SPEED, layer 0
                  self.encode_lc0(motor_bits) +
                  self.encode_lc1(speed))

        # Start the motor
        payload += bytes([0xA6, 0x00]) + self.encode_lc0(motor_bits)  # opOUTPUT_START

        return self.build_message(self.DIRECT_COMMAND_NO_REPLY, payload)

    @staticmethod
    def parse_reply(data):
        """Parse EV3 reply message"""
        if len(data) < 5:
            return None

        reply_size = struct.unpack('<H', data[0:2])[0]
        msg_counter = struct.unpack('<H', data[2:4])[0]
        reply_type = data[4]

        result = {
            'size': reply_size,
            'counter': msg_counter,
            'type': reply_type,
            'payload': data[5:] if len(data) > 5 else b''
        }

        return result





class SPPBluetoothApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.socket = None
        self.devices = {}  # Store device addresses with names

        # Create discovery agent in main thread
        self.discovery_agent = QBluetoothDeviceDiscoveryAgent()
        self.discovery_agent.deviceDiscovered.connect(self.on_device_discovered)
        self.discovery_agent.finished.connect(self.on_scan_finished)
        self.discovery_agent.errorOccurred.connect(self.on_scan_error)

        self.init_ui()

    def init_ui(self):
        self.setWindowTitle("PyQt6 Bluetooth SPP Application")
        self.setGeometry(100, 100, 700, 600)

        # Central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # Device discovery section
        discovery_layout = QHBoxLayout()
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(300)
        discovery_layout.addWidget(QLabel("Device:"))
        discovery_layout.addWidget(self.device_combo)

        self.scan_btn = QPushButton("Scan for Devices")
        self.scan_btn.clicked.connect(self.scan_devices)
        discovery_layout.addWidget(self.scan_btn)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self.connect_device)
        self.connect_btn.setEnabled(False)
        discovery_layout.addWidget(self.connect_btn)

        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.clicked.connect(self.disconnect_device)
        self.disconnect_btn.setEnabled(False)
        discovery_layout.addWidget(self.disconnect_btn)

        layout.addLayout(discovery_layout)

        # Status label
        self.status_label = QLabel("Status: Disconnected")
        layout.addWidget(self.status_label)

        # Received data display
        layout.addWidget(QLabel("Received Data:"))
        self.received_text = QTextEdit()
        self.received_text.setReadOnly(True)
        self.received_text.setMaximumHeight(200)
        layout.addWidget(self.received_text)

        # Send data section
        send_layout = QHBoxLayout()
        self.send_input = QLineEdit()
        self.send_input.setPlaceholderText("Enter text to send...")
        self.send_input.returnPressed.connect(self.send_data)
        send_layout.addWidget(self.send_input)

        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self.send_data)
        self.send_btn.setEnabled(False)
        send_layout.addWidget(self.send_btn)

        layout.addLayout(send_layout)

        # Log display
        layout.addWidget(QLabel("Log:"))
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        layout.addWidget(self.log_text)

        # Clear button
        clear_btn = QPushButton("Clear Log")
        clear_btn.clicked.connect(self.clear_log)
        layout.addWidget(clear_btn)

    def log(self, message):
        """Add message to log"""
        self.log_text.append(message)

    def scan_devices(self):
        """Start scanning for Bluetooth devices"""
        self.log("Starting device scan...")
        self.device_combo.clear()
        self.devices.clear()
        self.scan_btn.setEnabled(False)
        self.discovery_agent.start()

    def on_device_discovered(self, device):
        """Handle discovered device"""
        name = device.name() or "Unknown Device"
        address = device.address().toString()
        display_text = f"{name} ({address})"
        self.device_combo.addItem(display_text)
        self.devices[display_text] = address
        self.log(f"Found device: {display_text}")

    def on_scan_finished(self):
        """Handle scan completion"""
        self.log("Device scan completed")
        self.scan_btn.setEnabled(True)
        if self.device_combo.count() > 0:
            self.connect_btn.setEnabled(True)

    def on_scan_error(self, error):
        """Handle scan error"""
        self.log(f"Scan error: {error}")
        self.scan_btn.setEnabled(True)

    def connect_device(self):
        """Connect to selected device via SPP"""
        if self.device_combo.currentText() == "":
            return

        address = self.devices[self.device_combo.currentText()]
        self.log(f"Connecting to {address}...")

        # Create Bluetooth socket
        self.socket = QBluetoothSocket(QBluetoothServiceInfo.Protocol.RfcommProtocol)
        self.socket.connected.connect(self.on_connected)
        self.socket.disconnected.connect(self.on_disconnected)
        self.socket.readyRead.connect(self.on_data_received)
        self.socket.errorOccurred.connect(self.on_socket_error)

        # SPP UUID (Serial Port Profile)
        spp_uuid = QBluetoothUuid(QBluetoothUuid.ServiceClassUuid.SerialPort)

        # Connect to device
        self.socket.connectToService(QBluetoothAddress(address), spp_uuid)

        self.connect_btn.setEnabled(False)
        self.scan_btn.setEnabled(False)

    def on_connected(self):
        """Handle successful connection"""
        self.log("Connected successfully!")
        self.status_label.setText("Status: Connected")
        self.disconnect_btn.setEnabled(True)
        self.send_btn.setEnabled(True)

    def on_disconnected(self):
        """Handle disconnection"""
        self.log("Disconnected")
        self.status_label.setText("Status: Disconnected")
        self.disconnect_btn.setEnabled(False)
        self.send_btn.setEnabled(False)
        self.connect_btn.setEnabled(True)
        self.scan_btn.setEnabled(True)

    def on_socket_error(self, error):
        """Handle socket errors"""
        error_msg = self.socket.errorString()
        self.log(f"Socket error: {error_msg}")
        QMessageBox.warning(self, "Connection Error", error_msg)
        self.on_disconnected()

    def disconnect_device(self):
        """Disconnect from device"""
        if self.socket and self.socket.state() == QBluetoothSocket.SocketState.ConnectedState:
            self.log("Disconnecting...")
            self.socket.disconnectFromService()

    def on_data_received(self):
        """Handle received data"""
        if self.socket:
            data = self.socket.readAll()
            self.log(f"Raw data received: {len(data)} bytes")
            try:
                text = bytes(data).decode('utf-8')
                self.received_text.append(text)
                self.log(f"Received: {text}")
            except UnicodeDecodeError:
                hex_data = data.toHex().data().decode('ascii')
                self.received_text.append(f"[Binary: {hex_data}]")
                self.log(f"Received binary data: {hex_data}")

    def send_data(self):
        """Send data to connected device"""
        if not self.socket or self.socket.state() != QBluetoothSocket.SocketState.ConnectedState:
            QMessageBox.warning(self, "Not Connected", "Please connect to a device first")
            return

        text = self.send_input.text()
        if text:
            data = text.encode('utf-8')
            bytes_written = self.socket.write(data)
            if bytes_written > 0:
                self.log(f"Sent: {text} ({bytes_written} bytes)")
                self.send_input.clear()
            else:
                self.log("Failed to send data")
                QMessageBox.warning(self, "Send Error", "Failed to write data to socket")

    def clear_log(self):
        """Clear log text"""
        self.log_text.clear()
        self.received_text.clear()

    def closeEvent(self, event):
        """Handle window close"""
        if self.socket:
            self.socket.disconnectFromService()
        event.accept()


def main():
    app = QApplication(sys.argv)
    window = SPPBluetoothApp()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
