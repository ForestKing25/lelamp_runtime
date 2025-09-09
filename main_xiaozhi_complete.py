#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
完整的LeLamp XiaoZhi集成实现
基于py-xiaozhi-v4y.py集成MQTT和音频流处理
"""
import json
import time
import logging
import threading
import pyaudio
import subprocess
import requests
import socket
import ssl
from typing import Dict, Any, Optional

# XiaoZhi相关导入
import paho.mqtt.client as mqtt
import opuslib
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

# LeLamp硬件服务导入
from lelamp.service.motors.motors_service import MotorsService
from lelamp.service.rgb.rgb_service import RGBService

# ============ 配置 ==============
MAC_ADDR = 'ab:25:94:xx:xx:xx'  # 需要修改为实际MAC地址
OTA_VERSION_URL = 'https://api.tenclass.net/xiaozhi/ota/'

class LeLampXiaoZhiIntegration:
    """
    完整的LeLamp XiaoZhi集成
    """
    
    def __init__(self, port: str = "/dev/ttyACM0", lamp_id: str = "lelamp"):
        # 初始化硬件服务
        self.motors_service = MotorsService(port=port, lamp_id=lamp_id, fps=30)
        self.rgb_service = RGBService(
            led_count=40, led_pin=12, led_freq_hz=800000,
            led_dma=10, led_brightness=255, led_invert=False, led_channel=0
        )
        
        # XiaoZhi连接状态
        self.mqtt_client = None
        self.mqtt_info = {}
        self.aes_opus_info = {
            "type": "hello", "version": 3, "transport": "udp",
            "udp": {"server": "120.24.160.13", "port": 8884, 
                   "encryption": "aes-128-ctr",
                   "key": "263094c3aa28cb42f3965a1020cb21a7",
                   "nonce": "01000000ccba9720b4bc268100000000"},
            "audio_params": {"format": "opus", "sample_rate": 24000, "channels": 1, "frame_duration": 60},
            "session_id": None
        }
        
        # 音频和UDP
        self.audio = pyaudio.PyAudio()
        self.udp_socket = None
        self.local_sequence = 0
        self.listen_state = "stop"
        self.tts_state = None
        self.running = True
        
        # 线程管理
        self.recv_audio_thread = None
        self.send_audio_thread = None
        
        # 启动硬件服务
        self.motors_service.start()
        self.rgb_service.start()
        
        # 启动动画
        self.motors_service.dispatch("play", "wake_up")
        self.rgb_service.dispatch("solid", (255, 255, 255))
        self._set_system_volume(100)
        
        logging.info("LeLamp XiaoZhi集成初始化完成")

    def get_ota_version(self):
        """获取OTA版本信息和MQTT配置"""
        header = {'Device-Id': MAC_ADDR, 'Content-Type': 'application/json'}
        post_data = {
            "flash_size": 16777216, "minimum_free_heap_size": 8318916,
            "mac_address": MAC_ADDR, "chip_model_name": "esp32s3",
            "chip_info": {"model": 9, "cores": 2, "revision": 2, "features": 18},
            "application": {"name": "xiaozhi", "version": "0.9.9"},
            "board": {"type": "bread-compact-wifi", "ssid": "lelamp", "rssi": -50}
        }
        
        try:
            response = requests.post(OTA_VERSION_URL, headers=header, 
                                   data=json.dumps(post_data), timeout=10, verify=False)
            response.raise_for_status()
            self.mqtt_info = response.json()['mqtt']
            logging.info("MQTT配置获取成功")
            return True
        except Exception as e:
            logging.error(f"MQTT配置获取失败: {e}")
            return False

    def setup_mqtt(self):
        """设置MQTT连接"""
        self.mqtt_client = mqtt.Client(client_id=self.mqtt_info['client_id'])
        self.mqtt_client.username_pw_set(self.mqtt_info['username'], self.mqtt_info['password'])
        
        # SSL设置
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        self.mqtt_client.tls_set_context(context=ssl_context)
        
        # 设置回调
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_message
        self.mqtt_client.on_disconnect = self.on_mqtt_disconnect
        
        try:
            self.mqtt_client.connect(self.mqtt_info['endpoint'], 8883, 60)
            self.mqtt_client.loop_start()
            logging.info("MQTT连接成功")
            return True
        except Exception as e:
            logging.error(f"MQTT连接失败: {e}")
            return False

    def on_mqtt_connect(self, client, userdata, flags, rc):
        """MQTT连接回调"""
        if rc == 0:
            client.subscribe(self.mqtt_info['subscribe_topic'])
            # 发送IoT描述符
            self.send_iot_descriptors()
            logging.info("MQTT连接成功，已发送IoT描述符")
        else:
            logging.error(f"MQTT连接失败，错误码: {rc}")

    def on_mqtt_disconnect(self, client, userdata, rc):
        """MQTT断开连接回调"""
        logging.warning("MQTT连接断开，尝试重连...")
        time.sleep(5)
        client.reconnect()

    def on_mqtt_message(self, client, userdata, msg):
        """MQTT消息处理"""
        try:
            message = json.loads(msg.payload.decode('utf-8'))
            logging.info(f"收到MQTT消息: {message}")
            
            if message['type'] == 'hello':
                self.handle_hello_message(message)
            elif message['type'] == 'tts':
                self.tts_state = message['state']
            elif message['type'] == 'goodbye':
                self.handle_goodbye_message(message)
            elif message['type'] == 'iot_call':
                # 处理IoT方法调用
                self.handle_iot_call_message(message)
                
        except Exception as e:
            logging.error(f"MQTT消息处理错误: {e}")

    def handle_hello_message(self, message):
        """处理hello消息"""
        self.aes_opus_info['session_id'] = message.get('session_id')
        if 'udp' in message:
            self.aes_opus_info['udp'].update(message['udp'])
        
        # 重启音频流
        self.restart_audio_streams()
        logging.info(f"会话建立，ID: {self.aes_opus_info['session_id']}")

    def handle_goodbye_message(self, message):
        """处理goodbye消息"""
        if message.get('session_id') == self.aes_opus_info['session_id']:
            self.aes_opus_info['session_id'] = None
            self.stop_audio_streams()
            logging.info("会话结束")

    def handle_iot_call_message(self, message):
        """处理IoT方法调用消息"""
        try:
            device_name = message.get('device')
            method_name = message.get('method')
            parameters = message.get('parameters', {})
            call_id = message.get('call_id')
            
            # 执行IoT方法
            result = self.handle_iot_method_call(device_name, method_name, parameters)
            
            # 发送执行结果
            response = {
                "type": "iot_response",
                "call_id": call_id,
                "result": result,
                "session_id": self.aes_opus_info['session_id']
            }
            self.mqtt_client.publish(self.mqtt_info['publish_topic'], json.dumps(response))
            
        except Exception as e:
            logging.error(f"IoT方法调用处理错误: {e}")

    def send_iot_descriptors(self):
        """发送IoT设备描述符"""
        iot_msg = {
            "session_id": self.aes_opus_info['session_id'] or "init",
            "type": "iot",
            "descriptors": self.get_iot_descriptors()
        }
        self.mqtt_client.publish(self.mqtt_info['publish_topic'], json.dumps(iot_msg))

    def get_iot_descriptors(self) -> list:
        """获取IoT设备描述符"""
        return [
            {
                "name": "Motors",
                "description": "LeLamp的电机控制系统，用于物理表达和动作",
                "methods": {
                    "PlayRecording": {
                        "description": "播放指定的动作录制，让LeLamp做出相应的身体动作",
                        "parameters": {
                            "recording_name": {"description": "动作名称，可选值：curious, excited, happy_wiggle, headshake, nod, sad, scanning, shock, shy, wake_up", "type": "string"}
                        }
                    },
                    "GetAvailableRecordings": {
                        "description": "获取所有可用的动作录制列表",
                        "parameters": {}
                    }
                }
            },
            {
                "name": "RGB",
                "description": "LeLamp的RGB灯光系统，用于颜色表达和视觉效果",
                "methods": {
                    "SetSolid": {
                        "description": "设置纯色灯光，用于表达情感和状态",
                        "parameters": {
                            "red": {"description": "红色分量(0-255)", "type": "number"},
                            "green": {"description": "绿色分量(0-255)", "type": "number"},
                            "blue": {"description": "蓝色分量(0-255)", "type": "number"}
                        }
                    },
                    "PaintPattern": {
                        "description": "绘制复杂的灯光图案和动画效果",
                        "parameters": {
                            "colors": {"description": "40个RGB颜色的数组，每个颜色为[r,g,b]格式", "type": "array"}
                        }
                    }
                }
            },
            {
                "name": "Speaker",
                "description": "LeLamp的音响系统",
                "methods": {
                    "SetVolume": {
                        "description": "设置系统音量",
                        "parameters": {
                            "volume_percent": {"description": "音量百分比(0-100)", "type": "number"}
                        }
                    }
                }
            }
        ]

    def handle_iot_method_call(self, device_name: str, method_name: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """处理IoT方法调用"""
        try:
            if device_name == "Motors":
                return self._handle_motors_method(method_name, parameters)
            elif device_name == "RGB":
                return self._handle_rgb_method(method_name, parameters)
            elif device_name == "Speaker":
                return self._handle_speaker_method(method_name, parameters)
            else:
                return {"success": False, "error": f"未知设备: {device_name}"}
        except Exception as e:
            logging.error(f"IoT方法调用错误: {e}")
            return {"success": False, "error": str(e)}

    def _handle_motors_method(self, method_name: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """处理电机相关方法"""
        if method_name == "PlayRecording":
            recording_name = parameters.get("recording_name")
            if recording_name:
                self.motors_service.dispatch("play", recording_name)
                return {"success": True, "message": f"开始播放动作: {recording_name}"}
            return {"success": False, "error": "缺少recording_name参数"}
        
        elif method_name == "GetAvailableRecordings":
            recordings = self.motors_service.get_available_recordings()
            return {"success": True, "recordings": recordings}
        
        return {"success": False, "error": f"未知方法: {method_name}"}

    def _handle_rgb_method(self, method_name: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """处理RGB灯光相关方法"""
        if method_name == "SetSolid":
            red = parameters.get("red", 0)
            green = parameters.get("green", 0)
            blue = parameters.get("blue", 0)
            
            if all(0 <= val <= 255 for val in [red, green, blue]):
                self.rgb_service.dispatch("solid", (red, green, blue))
                return {"success": True, "message": f"设置灯光颜色: RGB({red}, {green}, {blue})"}
            return {"success": False, "error": "RGB值必须在0-255之间"}
        
        elif method_name == "PaintPattern":
            colors = parameters.get("colors", [])
            if isinstance(colors, list) and len(colors) == 40:
                validated_colors = []
                for color in colors:
                    if len(color) == 3 and all(0 <= val <= 255 for val in color):
                        validated_colors.append(tuple(color))
                    else:
                        return {"success": False, "error": "颜色格式错误"}
                
                self.rgb_service.dispatch("paint", validated_colors)
                return {"success": True, "message": f"绘制图案，共{len(validated_colors)}个颜色"}
            return {"success": False, "error": "需要40个RGB颜色数组"}
        
        return {"success": False, "error": f"未知方法: {method_name}"}

    def _handle_speaker_method(self, method_name: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """处理音响相关方法"""
        if method_name == "SetVolume":
            volume_percent = parameters.get("volume_percent", 50)
            if 0 <= volume_percent <= 100:
                self._set_system_volume(volume_percent)
                return {"success": True, "message": f"设置音量: {volume_percent}%"}
            return {"success": False, "error": "音量必须在0-100之间"}
        
        return {"success": False, "error": f"未知方法: {method_name}"}

    def _set_system_volume(self, volume_percent: int):
        """设置系统音量"""
        try:
            cmd_line_pcm = ["sudo", "-u", "pi", "amixer", "sset", "PCM", f"{volume_percent}%"]
            cmd_line_master = ["sudo", "-u", "pi", "amixer", "sset", "Master", f"{volume_percent}%"]
            
            subprocess.run(cmd_line_pcm, capture_output=True, text=True, timeout=5)
            subprocess.run(cmd_line_master, capture_output=True, text=True, timeout=5)
        except Exception as e:
            logging.error(f"音量设置错误: {e}")

    def restart_audio_streams(self):
        """重启音频流"""
        self.stop_audio_streams()
        
        try:
            # 创建UDP连接
            self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp_socket.settimeout(1)
            self.udp_socket.connect((
                self.aes_opus_info['udp']['server'],
                self.aes_opus_info['udp']['port']
            ))
            
            # 启动音频线程
            self.recv_audio_thread = threading.Thread(target=self.recv_audio, daemon=True)
            self.send_audio_thread = threading.Thread(target=self.send_audio, daemon=True)
            
            self.recv_audio_thread.start()
            self.send_audio_thread.start()
            
            logging.info("音频流启动成功")
        except Exception as e:
            logging.error(f"音频流启动失败: {e}")

    def stop_audio_streams(self):
        """停止音频流"""
        if self.udp_socket:
            self.udp_socket.close()
            self.udp_socket = None

    def aes_ctr_encrypt(self, key, nonce, plaintext):
        """AES CTR加密"""
        cipher = Cipher(algorithms.AES(key), modes.CTR(nonce), backend=default_backend())
        encryptor = cipher.encryptor()
        return encryptor.update(plaintext) + encryptor.finalize()

    def aes_ctr_decrypt(self, key, nonce, ciphertext):
        """AES CTR解密"""
        cipher = Cipher(algorithms.AES(key), modes.CTR(nonce), backend=default_backend())
        decryptor = cipher.decryptor()
        return decryptor.update(ciphertext) + decryptor.finalize()

    def send_audio(self):
        """发送音频数据"""
        key = self.aes_opus_info['udp']['key']
        nonce = self.aes_opus_info['udp']['nonce']
        server_ip = self.aes_opus_info['udp']['server']
        server_port = self.aes_opus_info['udp']['port']

        encoder = opuslib.Encoder(16000, 1, opuslib.APPLICATION_AUDIO)
        mic = self.audio.open(format=pyaudio.paInt16, channels=1, rate=16000, input=True, frames_per_buffer=960)

        try:
            while self.running and self.aes_opus_info['session_id']:
                if self.listen_state == "stop":
                    time.sleep(0.1)
                    continue

                data = mic.read(960)
                encoded_data = encoder.encode(data, 960)

                self.local_sequence += 1
                new_nonce = nonce[0:4] + format(len(encoded_data), '04x') + nonce[8:24] + format(self.local_sequence, '08x')

                encrypt_encoded_data = self.aes_ctr_encrypt(
                    bytes.fromhex(key), bytes.fromhex(new_nonce), bytes(encoded_data)
                )
                data = bytes.fromhex(new_nonce) + encrypt_encoded_data
                
                if self.udp_socket:
                    self.udp_socket.sendto(data, (server_ip, server_port))
        except Exception as e:
            logging.error(f"音频发送错误: {e}")
        finally:
            mic.stop_stream()
            mic.close()

    def recv_audio(self):
        """接收音频数据"""
        key = self.aes_opus_info['udp']['key']
        sample_rate = self.aes_opus_info['audio_params']['sample_rate']
        frame_duration = self.aes_opus_info['audio_params']['frame_duration']
        frame_num = int(frame_duration / (1000 / sample_rate))

        decoder = opuslib.Decoder(sample_rate, 1)
        spk = self.audio.open(format=pyaudio.paInt16, channels=1, rate=sample_rate, output=True, frames_per_buffer=frame_num)

        try:
            while self.running and self.aes_opus_info['session_id']:
                if not self.udp_socket:
                    time.sleep(0.1)
                    continue
                    
                try:
                    data, server = self.udp_socket.recvfrom(4096)
                    split_nonce = data[:16]
                    encrypt_data = data[16:]

                    decrypt_data = self.aes_ctr_decrypt(bytes.fromhex(key), split_nonce, encrypt_data)
                    spk.write(decoder.decode(decrypt_data, frame_num))
                except socket.timeout:
                    continue
                except Exception as e:
                    logging.error(f"音频接收错误: {e}")
                    break
        finally:
            spk.stop_stream()
            spk.close()

    def send_listen_start(self):
        """发送开始监听消息"""
        if self.aes_opus_info['session_id']:
            msg = {
                "session_id": self.aes_opus_info['session_id'],
                "type": "listen",
                "state": "start",
                "mode": "manual"
            }
            self.mqtt_client.publish(self.mqtt_info['publish_topic'], json.dumps(msg))
            self.listen_state = "start"

    def send_listen_stop(self):
        """发送停止监听消息"""
        if self.aes_opus_info['session_id']:
            msg = {
                "session_id": self.aes_opus_info['session_id'],
                "type": "listen",
                "state": "stop"
            }
            self.mqtt_client.publish(self.mqtt_info['publish_topic'], json.dumps(msg))
            self.listen_state = "stop"

    def start(self):
        """启动XiaoZhi集成"""
        # 获取配置
        if not self.get_ota_version():
            return False
        
        # 建立MQTT连接
        if not self.setup_mqtt():
            return False
        
        # 发送hello消息建立会话
        hello_msg = {
            "type": "hello",
            "version": 3,
            "transport": "udp",
            "audio_params": {
                "format": "opus",
                "sample_rate": 16000,
                "channels": 1,
                "frame_duration": 60
            }
        }
        self.mqtt_client.publish(self.mqtt_info['publish_topic'], json.dumps(hello_msg))
        
        logging.info("LeLamp XiaoZhi集成启动成功")
        return True

    def stop(self):
        """停止服务"""
        self.running = False
        self.stop_audio_streams()
        
        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
        
        if self.motors_service:
            self.motors_service.stop()
        if self.rgb_service:
            self.rgb_service.stop()
        if self.audio:
            self.audio.terminate()
        
        logging.info("LeLamp XiaoZhi集成已停止")

def main():
    """主函数"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('lelamp_xiaozhi.log'),
            logging.StreamHandler()
        ]
    )
    
    try:
        lelamp = LeLampXiaoZhiIntegration()
        
        if lelamp.start():
            print("LeLamp XiaoZhi集成启动成功！")
            print("现在可以通过小智AI与LeLamp进行语音交互")
            
            # 保持运行
            while True:
                time.sleep(1)
        else:
            print("启动失败")
            
    except KeyboardInterrupt:
        print("程序中断")
    finally:
        if 'lelamp' in locals():
            lelamp.stop()

if __name__ == "__main__":
    main()
