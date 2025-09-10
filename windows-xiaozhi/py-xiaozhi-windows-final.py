#!/usr/bin/python
# -*- coding: UTF-8 -*-
import json
import time
import requests
import paho.mqtt.client as mqtt
import paho.mqtt.subscribe as subscribe
import paho.mqtt.publish as publish
import threading
import pyaudio
import socket
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
import logging
# Windows 兼容：移除 RPi.GPIO 和 opuslib
from tkinter import Tk, scrolledtext, Frame, END, Button, Label
from tkinter.font import Font
import queue
import os
import errno
import ssl  # 添加 SSL 模块
import platform

# Windows 环境适配
if platform.system() == "Windows":
    print("检测到 Windows 环境，使用 Windows 兼容模式")
else:
    # 如果不是 Windows，设置 X11 环境变量
    os.environ['DISPLAY'] = ':0'
    os.environ['XAUTHORITY'] = '/home/pi/.Xauthority'

# 设置日志记录
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    filename='app.log',
    filemode='w'
)

# ============ Windows 兼容的输入配置 ==============

button_pressed = False
recording = False

# ============ 全局变量 ==============

OTA_VERSION_URL = 'https://api.tenclass.net/xiaozhi/ota/'
MAC_ADDR = 'ab:25:94:23:4d:55' # 自己修改一个

mqtt_info = {}
aes_opus_info = {
    "type": "hello",
    "version": 3,
    "transport": "udp",
    "udp": {
        "server": "120.24.160.13",  # 使用本地测试服务器
        "port": 8884,
        "encryption": "aes-128-ctr",
        "key": "263094c3aa28cb42f3965a1020cb21a7",
        "nonce": "01000000ccba9720b4bc268100000000"
    },
    "audio_params": {
        "format": "raw",  # 使用原始音频格式而不是 opus
        "sample_rate": 24000,
        "channels": 1,
        "frame_duration": 60
    },
    "session_id": "b23ebfe9"
}

iot_msg = {"session_id": "635aa42d", "type": "iot",
           "descriptors": [{"name": "Speaker", "description": "当前 AI 机器人的扬声器",
                            "properties": {"volume": {"description": "当前音量值", "type": "number"}},
                            "methods": {"SetVolume": {"description": "设置音量",
                                                      "parameters": {
                                                          "volume": {"description": "0到100之间的整数", "type": "number"}
                                                      }
                                                      }
                                        }
                           },
                           {"name": "Lamp", "description": "一个测试用的灯",
                            "properties": {"power": {"description": "灯是否打开", "type": "boolean"}},
                            "methods": {"TurnOn": {"description": "打开灯", "parameters": {}},
                                        "TurnOff": {"description": "关闭灯", "parameters": {}}
                                        }
                           }
                           ]
           }

iot_status_msg = {
    "session_id": "635aa42d",
    "type": "iot",
    "states": [
        {"name": "Speaker", "state": {"volume": 50}},
        {"name": "Lamp", "state": {"power": False}}
    ]
}

goodbye_msg = {
    "session_id": "b23ebfe9",
    "type": "goodbye"
}

local_sequence = 0
listen_state = None
tts_state = None
key_state = None
audio = None
udp_socket = None
conn_state = False
running = True
last_heartbeat = 0
last_listen_stop_time = None
socket_lock = threading.Lock()  # 保护socket操作的锁

# 线程管理
recv_audio_thread = None
send_audio_thread = None
mqtt_client = None

# ============ GUI 更新队列 ==============

GUI_UPDATE_QUEUE = queue.Queue()
MONITOR_UPDATE_QUEUE = queue.Queue()  # 新增监控数据队列
RECONNECT_INTERVAL = 5
HEARTBEAT_INTERVAL = 30

# ============ Windows GUI 界面 ==============

class ChatWindow:
    def __init__(self, master):
        self.master = master
        master.title("小智聊天机器人 - Windows版本")
        master.geometry("1200x800")
        
        # 设置窗口属性
        master.resizable(True, True)
        
        # 尝试设置图标（可选）
        try:
            master.iconbitmap(default='icon.ico')
        except:
            pass

        # 创建字体
        try:
            self.title_font = Font(family="Microsoft YaHei", size=16, weight="bold")
            self.text_font = Font(family="Microsoft YaHei", size=11)
            self.button_font = Font(family="Microsoft YaHei", size=12, weight="bold")
            self.status_font = Font(family="Consolas", size=9)
        except:
            self.title_font = Font(family="Arial", size=16, weight="bold")
            self.text_font = Font(family="Arial", size=11)
            self.button_font = Font(family="Arial", size=12, weight="bold")
            self.status_font = Font(family="Courier", size=9)

        self.setup_ui()

    def setup_ui(self):
        """设置用户界面"""
        # 主容器
        main_container = Frame(self.master)
        main_container.pack(expand=True, fill="both", padx=15, pady=15)

        # 标题区域
        title_frame = Frame(main_container)
        title_frame.pack(fill="x", pady=(0, 15))
        
        title_label = Label(
            title_frame,
            text="🤖 小智聊天机器人 (Windows版)",
            font=self.title_font,
            fg="#1976D2"
        )
        title_label.pack()

        # 状态显示区域
        status_frame = Frame(main_container)
        status_frame.pack(fill="x", pady=(0, 10))
        
        Label(status_frame, text="系统状态:", font=self.text_font, fg="#424242").pack(anchor="w")
        
        self.status_text = scrolledtext.ScrolledText(
            status_frame,
            height=5,
            wrap="word",
            font=self.status_font,
            bg="#f5f5f5",
            fg="#424242"
        )
        self.status_text.pack(fill="x")
        
        # 聊天区域
        chat_frame = Frame(main_container)
        chat_frame.pack(expand=True, fill="both", pady=(0, 10))
        
        Label(chat_frame, text="聊天记录:", font=self.text_font, fg="#424242").pack(anchor="w")
        
        self.text_area = scrolledtext.ScrolledText(
            chat_frame,
            wrap="word",
            font=self.text_font,
            state='disabled',
            bg="white",
            fg="#212121",
            height=12
        )
        self.text_area.pack(expand=True, fill="both")
        
        # 数据传输监控区域
        monitor_frame = Frame(main_container)
        monitor_frame.pack(fill="both", expand=True, pady=(0, 10))
        
        Label(monitor_frame, text="数据传输监控:", font=self.text_font, fg="#424242").pack(anchor="w")
        
        self.monitor_text = scrolledtext.ScrolledText(
            monitor_frame,
            wrap="word",
            font=Font(family="Consolas", size=8),
            state='disabled',
            bg="#1e1e1e",
            fg="#00ff00",
            height=8
        )
        self.monitor_text.pack(expand=True, fill="both")

        # 控制按钮区域
        self.setup_buttons(main_container)
        
        # 初始化状态
        self.update_status("🚀 程序启动完成")
        self.update_status("💡 点击'测试音频'检查音频设备")
        self.update_status("🌐 点击'连接服务器'开始连接")
        self.update_status("🎤 使用空格键或按钮控制录音")
        
        # 初始化监控区域
        self.append_monitor_data("system", "监控系统启动", 0, "数据传输监控已启动，将显示所有发送和接收的数据")

    def setup_buttons(self, parent):
        """设置控制按钮"""
        button_container = Frame(parent)
        button_container.pack(fill="x", pady=(15, 0))
        
        # 第一行按钮
        row1 = Frame(button_container)
        row1.pack(fill="x", pady=(0, 8))
        
        self.test_audio_button = Button(
            row1,
            text="🔊 测试音频",
            command=self.test_audio,
            font=self.button_font,
            bg="#FF9800",
            fg="white",
            width=15,
            relief="raised",
            bd=2
        )
        self.test_audio_button.pack(side="left", padx=(0, 10))
        
        self.connect_button = Button(
            row1,
            text="🌐 连接服务器",
            command=self.connect_to_server,
            font=self.button_font,
            bg="#2196F3",
            fg="white",
            width=15,
            relief="raised",
            bd=2
        )
        self.connect_button.pack(side="left", padx=(0, 10))
        
        self.test_output_button = Button(
            row1,
            text="🔈 测试输出",
            command=self.test_audio_output,
            font=self.button_font,
            bg="#9C27B0",
            fg="white",
            width=15,
            relief="raised",
            bd=2
        )
        self.test_output_button.pack(side="left", padx=(0, 10))
        
        # 第二行按钮
        row2 = Frame(button_container)
        row2.pack(fill="x")
        
        self.record_button = Button(
            row2,
            text="🎤 开始录音",
            command=self.toggle_recording,
            font=self.button_font,
            bg="#4CAF50",
            fg="white",
            width=15,
            relief="raised",
            bd=2,
            state="disabled"
        )
        self.record_button.pack(side="left", padx=(0, 10))
        
        self.quit_button = Button(
            row2,
            text="❌ 退出程序",
            command=self.quit_app,
            font=self.button_font,
            bg="#f44336",
            fg="white",
            width=15,
            relief="raised",
            bd=2
        )
        self.quit_button.pack(side="right")
        
        # 绑定键盘事件
        self.master.bind('<KeyPress-space>', self.on_space_key)
        self.master.focus_set()

    def test_audio(self):
        """测试音频设备"""
        self.test_audio_button.config(state="disabled", text="🔄 测试中...")
        self.update_status("🔊 开始测试音频设备...")
        
        def test_thread():
            try:
                result = get_microphone()
                self.safe_after(lambda: self.on_audio_test_complete(result))
            except Exception as e:
                self.safe_after(lambda: self.on_audio_test_error(str(e)))
        
        threading.Thread(target=test_thread, daemon=True).start()

    def on_audio_test_complete(self, success):
        """音频测试完成"""
        self.test_audio_button.config(state="normal", text="🔊 测试音频")
        if success:
            self.update_status("✅ 音频设备测试成功")
        else:
            self.update_status("❌ 音频设备测试失败")

    def on_audio_test_error(self, error):
        """音频测试错误"""
        self.test_audio_button.config(state="normal", text="🔊 测试音频")
        self.update_status(f"❌ 音频测试错误: {error}")

    def test_audio_output(self):
        """测试音频输出设备"""
        self.test_output_button.config(state="disabled", text="🔄 测试中...")
        self.update_status("🔈 开始测试音频输出设备...")
        
        def test_output_thread():
            try:
                result = test_audio_playback()
                self.safe_after(lambda: self.on_output_test_complete(result))
            except Exception as e:
                self.safe_after(lambda: self.on_output_test_error(str(e)))
        
        threading.Thread(target=test_output_thread, daemon=True).start()

    def on_output_test_complete(self, success):
        """音频输出测试完成"""
        self.test_output_button.config(state="normal", text="🔈 测试输出")
        if success:
            self.update_status("✅ 音频输出设备测试成功")
        else:
            self.update_status("❌ 音频输出设备测试失败")

    def on_output_test_error(self, error):
        """音频输出测试错误"""
        self.test_output_button.config(state="normal", text="🔈 测试输出")
        self.update_status(f"❌ 音频输出测试错误: {error}")

    def connect_to_server(self):
        """连接到服务器"""
        self.connect_button.config(state="disabled", text="🔄 连接中...")
        self.update_status("🌐 正在连接服务器...")
        
        def connect_thread():
            try:
                success = connect_udp()
                if success:
                    send_hello()
                # 使用安全的方式调用 GUI 更新
                self.safe_after(lambda: self.on_connect_complete(success))
            except Exception as e:
                self.safe_after(lambda: self.on_connect_error(str(e)))
        
        threading.Thread(target=connect_thread, daemon=True).start()
    
    def safe_after(self, callback):
        """安全的线程间GUI调用"""
        try:
            if self.master and self.master.winfo_exists():
                self.master.after(0, callback)
        except Exception as e:
            print(f"GUI回调错误: {e}")
            # 如果GUI调用失败，使用队列方式
            try:
                add_gui_message("连接状态更新失败", "system")
            except:
                pass

    def on_connect_complete(self, success):
        """连接完成"""
        if success:
            self.connect_button.config(
                state="normal", 
                text="🔄 重新连接", 
                bg="#4CAF50"
            )
            self.record_button.config(state="normal")
            self.update_status("✅ 服务器连接成功！现在可以开始录音")
        else:
            self.connect_button.config(
                state="normal", 
                text="🔄 重试连接", 
                bg="#f44336"
            )
            self.update_status("❌ 服务器连接失败")

    def on_connect_error(self, error):
        """连接错误"""
        self.connect_button.config(state="normal", text="🔄 重试连接", bg="#f44336")
        self.update_status(f"❌ 连接错误: {error}")

    def toggle_recording(self):
        """切换录音状态"""
        global recording
        recording = not recording
        
        if recording:
            self.record_button.config(text="⏹️ 停止录音", bg="#f44336")
            self.append_text("开始录音...", "user")
            self.update_status("🎤 正在录音...")
        else:
            self.record_button.config(text="🎤 开始录音", bg="#4CAF50")
            self.append_text("停止录音", "user")
            self.update_status("⏹️ 录音已停止")

    def on_space_key(self, event):
        """处理空格键"""
        if self.record_button['state'] != 'disabled':
            self.toggle_recording()

    def quit_app(self):
        """退出应用"""
        global running
        running = False
        self.update_status("🔄 正在退出程序...")
        
        # 延迟关闭，给状态更新时间
        def delayed_quit():
            try:
                self.master.quit()
            except:
                pass
        
        try:
            self.master.after(500, delayed_quit)
        except:
            # 如果after调用失败，直接退出
            try:
                self.master.quit()
            except:
                pass

    def update_status(self, text):
        """更新状态"""
        timestamp = time.strftime('%H:%M:%S')
        self.status_text.config(state='normal')
        self.status_text.insert(END, f"[{timestamp}] {text}\n")
        self.status_text.see(END)
        self.status_text.config(state='disabled')

    def append_text(self, text, sender="system"):
        """添加聊天文本"""
        timestamp = time.strftime('%H:%M:%S')
        self.text_area.config(state='normal')
        
        if sender == "ai":
            self.text_area.insert(END, f"[{timestamp}] 🤖 AI: {text}\n")
        elif sender == "user":
            self.text_area.insert(END, f"[{timestamp}] 👤 用户: {text}\n")
        else:
            self.text_area.insert(END, f"[{timestamp}] ⚙️ 系统: {text}\n")
        
        self.text_area.see(END)
        self.text_area.config(state='disabled')

    def append_monitor_data(self, direction, data_type, size, content_preview=""):
        """添加数据传输监控信息"""
        # 获取包含毫秒的时间戳
        now = time.time()
        timestamp = time.strftime('%H:%M:%S', time.localtime(now))
        milliseconds = int((now % 1) * 1000)
        full_timestamp = f"{timestamp}.{milliseconds:03d}"
        
        # 方向图标和颜色
        if direction == "send":
            icon = "📤 发送"
            color_tag = "send"
        elif direction == "recv":
            icon = "📥 接收"
            color_tag = "recv"
        else:  # system
            icon = "⚙️ 系统"
            color_tag = "system"
        
        self.monitor_text.config(state='normal')
        
        # 插入时间戳和方向
        self.monitor_text.insert(END, f"[{full_timestamp}] {icon} ", color_tag)
        
        # 插入数据类型和大小
        if size > 0:
            self.monitor_text.insert(END, f"{data_type} ({size} 字节)\n")
        else:
            self.monitor_text.insert(END, f"{data_type}\n")
        
        # 如果有内容预览，显示前150个字符
        if content_preview:
            preview = content_preview[:150]
            if len(content_preview) > 150:
                preview += "..."
            self.monitor_text.insert(END, f"  内容: {preview}\n")
        
        self.monitor_text.insert(END, f"  {'='*60}\n")
        
        # 配置颜色标签
        self.monitor_text.tag_config("send", foreground="#ff6b6b")      # 红色 - 发送
        self.monitor_text.tag_config("recv", foreground="#4ecdc4")      # 青色 - 接收
        self.monitor_text.tag_config("system", foreground="#ffd93d")    # 黄色 - 系统
        
        self.monitor_text.see(END)
        self.monitor_text.config(state='disabled')
        
        # 限制监控区域的行数，避免内存占用过多
        lines = self.monitor_text.get("1.0", END).count('\n')
        if lines > 300:  # 保留最新的300行
            self.monitor_text.config(state='normal')
            self.monitor_text.delete("1.0", "50.0")  # 删除前50行
            self.monitor_text.config(state='disabled')

chat_window = None

def update_gui():
    """更新GUI"""
    global chat_window
    if chat_window is None:
        return
        
    try:
        # 更新聊天消息
        while not GUI_UPDATE_QUEUE.empty():
            message, sender = GUI_UPDATE_QUEUE.get_nowait()
            chat_window.append_text(message, sender)
            
        # 更新监控数据
        while not MONITOR_UPDATE_QUEUE.empty():
            direction, data_type, size, content = MONITOR_UPDATE_QUEUE.get_nowait()
            chat_window.append_monitor_data(direction, data_type, size, content)
            
    except queue.Empty:
        pass

def add_gui_message(message, sender="system"):
    """添加GUI消息"""
    GUI_UPDATE_QUEUE.put((message, sender))

def add_monitor_data(direction, data_type, size, content_preview=""):
    """添加监控数据"""
    MONITOR_UPDATE_QUEUE.put((direction, data_type, size, content_preview))

# ============ 音频处理 ==============

def get_microphone():
    """获取麦克风设备"""
    global audio
    try:
        if audio is None:
            audio = pyaudio.PyAudio()
        
        print("\n=== 音频设备检测 ===")
        device_found = False
        input_devices = []
        
        for i in range(audio.get_device_count()):
            info = audio.get_device_info_by_index(i)
            if info['maxInputChannels'] > 0:
                print(f"输入设备 {i}: {info['name']} (通道: {info['maxInputChannels']})")
                input_devices.append((i, info))
                device_found = True
        
        if not device_found:
            print("❌ 未找到可用的音频输入设备")
            return False
        
        # 获取默认设备
        try:
            default_device = audio.get_default_input_device_info()
            print(f"✅ 默认输入设备: {default_device['name']}")
            return True
        except Exception as e:
            print(f"⚠️ 获取默认设备失败: {e}")
            if input_devices:
                print(f"✅ 使用第一个可用设备: {input_devices[0][1]['name']}")
                return True
            return False
        
    except Exception as e:
        print(f"❌ 音频设备检测失败: {e}")
        return False

def test_audio_playback():
    """测试音频输出设备"""
    global audio
    try:
        if audio is None:
            audio = pyaudio.PyAudio()
        
        print("\n=== 音频输出设备检测 ===")
        output_devices = []
        
        # 检测输出设备
        for i in range(audio.get_device_count()):
            info = audio.get_device_info_by_index(i)
            if info['maxOutputChannels'] > 0:
                print(f"输出设备 {i}: {info['name']} (通道: {info['maxOutputChannels']})")
                output_devices.append((i, info))
        
        if not output_devices:
            print("❌ 未找到可用的音频输出设备")
            return False
        
        # 获取默认输出设备
        try:
            default_output = audio.get_default_output_device_info()
            print(f"✅ 默认输出设备: {default_output['name']}")
        except Exception as e:
            print(f"⚠️ 获取默认输出设备失败: {e}")
            default_output = output_devices[0][1]
            print(f"✅ 使用第一个可用输出设备: {default_output['name']}")
        
        # 生成测试音频（正弦波）
        import numpy as np
        
        SAMPLE_RATE = 44100
        DURATION = 1.0  # 1秒
        FREQUENCY = 440  # A音符
        
        # 生成正弦波
        t = np.linspace(0, DURATION, int(SAMPLE_RATE * DURATION), False)
        wave = np.sin(FREQUENCY * 2 * np.pi * t)
        
        # 添加渐入渐出效果，避免爆音
        fade_samples = int(0.1 * SAMPLE_RATE)  # 0.1秒渐变
        wave[:fade_samples] *= np.linspace(0, 1, fade_samples)
        wave[-fade_samples:] *= np.linspace(1, 0, fade_samples)
        
        # 转换为int16格式
        audio_data = (wave * 32767).astype(np.int16)
        
        # 播放测试音频
        print("🔈 播放测试音频（440Hz正弦波，1秒）...")
        
        stream = audio.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=SAMPLE_RATE,
            output=True,
            frames_per_buffer=1024
        )
        
        # 分块播放
        chunk_size = 1024
        for i in range(0, len(audio_data), chunk_size):
            chunk = audio_data[i:i+chunk_size]
            if len(chunk) < chunk_size:
                # 最后一块，填充零
                chunk = np.pad(chunk, (0, chunk_size - len(chunk)), mode='constant')
            stream.write(chunk.tobytes())
        
        stream.stop_stream()
        stream.close()
        
        print("✅ 音频输出测试完成")
        return True
        
    except ImportError:
        print("❌ 需要安装numpy库进行音频输出测试")
        print("    请运行: pip install numpy")
        return False
    except Exception as e:
        print(f"❌ 音频输出测试失败: {e}")
        return False

def init_audio():
    """初始化音频"""
    global audio
    try:
        if audio is None:
            audio = pyaudio.PyAudio()
        
        result = get_microphone()
        if result:
            add_gui_message("音频设备初始化成功", "system")
        else:
            add_gui_message("音频设备初始化失败", "system")
        return result
    except Exception as e:
        add_gui_message(f"音频初始化错误: {e}", "system")
        return False

# ============ 网络连接 ==============

def connect_udp():
    """连接UDP"""
    global udp_socket, conn_state
    try:
        with socket_lock:
            # 关闭旧连接
            if udp_socket:
                udp_socket.close()
            
            udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp_socket.settimeout(10.0)
        
        server = aes_opus_info["udp"]["server"]
        port = aes_opus_info["udp"]["port"]
        
        print(f"正在测试连接 {server}:{port}")
        
        # 发送测试数据验证连接
        test_msg = {"type": "ping", "timestamp": time.time()}
        test_data = json.dumps(test_msg).encode('utf-8')
        udp_socket.sendto(test_data, (server, port))
        
        # 尝试接收响应（可选，用于验证服务器是否可达）
        try:
            udp_socket.settimeout(3.0)  # 3秒超时
            response = udp_socket.recv(1024)
            print(f"收到服务器响应: {len(response)} 字节")
        except socket.timeout:
            print("⚠️ 未收到服务器响应，但连接已建立")
        except Exception as e:
            print(f"⚠️ 接收响应时出错: {e}")
        
        conn_state = True
        print(f"✅ UDP连接成功")
        add_gui_message(f"服务器连接成功: {server}:{port}", "system")
        return True
        
    except Exception as e:
        print(f"❌ UDP连接失败: {e}")
        add_gui_message(f"服务器连接失败: {e}", "system")
        conn_state = False
        return False

def send_hello():
    """发送hello消息"""
    if not conn_state or not udp_socket:
        return False
    
    try:
        server = aes_opus_info["udp"]["server"]
        port = aes_opus_info["udp"]["port"]
        hello_data = json.dumps(aes_opus_info).encode('utf-8')
        
        # 记录发送数据
        add_monitor_data("send", "Hello消息", len(hello_data), hello_data.decode('utf-8'))
        
        # 使用 sendto 方法发送到指定地址
        udp_socket.sendto(hello_data, (server, port))
        print("✅ Hello消息发送成功")
        add_gui_message("Hello消息发送成功", "system")
        return True
    except Exception as e:
        print(f"❌ Hello发送失败: {e}")
        add_gui_message(f"Hello发送失败: {e}", "system")
        add_monitor_data("send", "Hello消息 [失败]", 0, f"错误: {e}")
        return False

def handle_audio_recv():
    """音频接收处理"""
    global running, udp_socket
    print("🎧 音频接收线程启动")
    
    # 等待连接建立
    wait_count = 0
    while running and (not conn_state or not udp_socket):
        time.sleep(0.5)  # 等待连接建立
        wait_count += 1
        if wait_count % 10 == 0:  # 每5秒打印一次等待状态
            print(f"🎧 音频接收线程：等待连接建立... (conn_state={conn_state}, udp_socket={udp_socket is not None})")
        if not running:
            print("🎧 音频接收线程：程序正在退出，停止等待")
            return
    
    print("🎧 音频接收线程：连接已建立，开始监听数据")
    
    while running and conn_state:
        try:
            # 使用锁保护socket操作
            with socket_lock:
                if not udp_socket or not conn_state:
                    break
                current_socket = udp_socket  # 保存当前socket引用
            
            current_socket.settimeout(2.0)
            data = current_socket.recv(8192)
            
            if len(data) > 0:
                # 尝试解析数据类型
                data_type = "未知数据"
                content_preview = ""
                
                try:
                    # 尝试解析为JSON
                    json_data = json.loads(data.decode('utf-8'))
                    data_type = f"JSON消息 (type: {json_data.get('type', 'unknown')})"
                    content_preview = data.decode('utf-8')
                    
                    # 特殊处理不同类型的消息
                    msg_type = json_data.get('type', '')
                    if msg_type == 'tts':
                        add_gui_message(f"AI 说: {json_data.get('text', '...')}", "ai")
                    elif msg_type == 'listen':
                        add_gui_message("AI 开始监听", "ai")
                    elif msg_type == 'hello':
                        add_gui_message("收到服务器Hello响应", "system")
                        
                except UnicodeDecodeError:
                    # 可能是音频数据
                    data_type = "音频数据"
                    content_preview = f"二进制数据 (前16字节): {data[:16].hex()}"
                except json.JSONDecodeError:
                    # 其他文本数据
                    try:
                        content_preview = data.decode('utf-8', errors='ignore')
                        data_type = "文本数据"
                    except:
                        data_type = "二进制数据"
                        content_preview = f"十六进制: {data[:32].hex()}"
                
                # 记录接收数据
                add_monitor_data("recv", data_type, len(data), content_preview)
                
                if len(data) > 10:  # 忽略小数据包的日志
                    print(f"📥 收到数据: {data_type} ({len(data)} 字节)")
                
        except socket.timeout:
            continue
        except Exception as e:
            print(f"❌ 音频接收错误: {e}")
            add_monitor_data("recv", "接收错误", 0, f"错误: {e}")
            break
    
    print("🎧 音频接收线程结束")

def handle_audio_send():
    """音频发送处理"""
    global running, recording, audio
    print("🎤 音频发送线程启动")
    
    if not init_audio():
        print("❌ 音频初始化失败，发送线程退出")
        return
    
    # 音频参数
    CHUNK = 1024
    FORMAT = pyaudio.paInt16
    CHANNELS = 1
    RATE = 16000  # 降低采样率以减少数据量
    
    stream = None
    
    try:
        stream = audio.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=RATE,
            input=True,
            frames_per_buffer=CHUNK
        )
        
        print("✅ 音频流创建成功")
        add_gui_message("音频流准备就绪", "system")
        
        while running:
            if recording and conn_state and udp_socket:
                try:
                    # 读取音频数据
                    audio_data = stream.read(CHUNK, exception_on_overflow=False)
                    
                    if len(audio_data) > 0:
                        # 发送原始音频数据到指定地址
                        server = aes_opus_info["udp"]["server"]
                        port = aes_opus_info["udp"]["port"]
                        udp_socket.sendto(audio_data, (server, port))
                        
                        # 记录发送的音频数据
                        add_monitor_data("send", "音频数据", len(audio_data), 
                                       f"PCM音频 {RATE}Hz {CHANNELS}声道")
                        
                        print(f"📤 发送音频: {len(audio_data)} 字节")
                        
                except Exception as e:
                    print(f"❌ 录音错误: {e}")
                    add_gui_message(f"录音错误: {e}", "system")
                    add_monitor_data("send", "音频数据 [错误]", 0, f"错误: {e}")
            
            time.sleep(0.02)  # 50Hz 更新频率
    
    except Exception as e:
        print(f"❌ 音频流错误: {e}")
        add_gui_message(f"音频流错误: {e}", "system")
    finally:
        if stream:
            stream.stop_stream()
            stream.close()
            print("🎤 音频流已关闭")
        
        print("🎤 音频发送线程结束")

# ============ 主程序 ==============

def main():
    """主程序"""
    global running, chat_window, recv_audio_thread, send_audio_thread
    
    print("🚀 启动小智聊天机器人 Windows 版")
    print(f"Python 版本: {platform.python_version()}")
    print(f"操作系统: {platform.system()} {platform.release()}")
    
    try:
        # 创建GUI
        root = Tk()
        chat_window = ChatWindow(root)
        
        print("✅ GUI界面创建成功")
        add_gui_message("界面初始化完成", "system")
        
        # 启动音频处理线程
        send_audio_thread = threading.Thread(target=handle_audio_send, daemon=True)
        recv_audio_thread = threading.Thread(target=handle_audio_recv, daemon=True)
        
        send_audio_thread.start()
        recv_audio_thread.start()
        
        print("✅ 后台线程启动成功")
        
        # 定期更新GUI的函数
        def periodic_update():
            if running:
                try:
                    update_gui()
                    root.after(10, periodic_update)  # 每10ms更新一次
                except Exception as e:
                    print(f"GUI更新错误: {e}")
        
        # 启动定期更新
        periodic_update()
        
        # 设置窗口关闭事件
        def on_closing():
            global running
            running = False
            try:
                root.quit()
                root.destroy()
            except:
                pass
        
        root.protocol("WM_DELETE_WINDOW", on_closing)
        
        # 使用标准的 tkinter 主循环
        try:
            root.mainloop()
        except Exception as e:
            print(f"主循环错误: {e}")
    
    except KeyboardInterrupt:
        print("\n⚠️ 用户中断程序")
    
    except Exception as e:
        print(f"❌ 程序错误: {e}")
    
    finally:
        print("🔄 开始清理资源...")
        running = False
        
        # 清理音频资源
        if audio:
            try:
                audio.terminate()
                print("✅ 音频资源已清理")
            except:
                pass
        
        # 清理网络连接
        if udp_socket:
            try:
                udp_socket.close()
                print("✅ 网络连接已关闭")
            except:
                pass
        
        # 清理GUI
        try:
            root.quit()
            root.destroy()
            print("✅ GUI已关闭")
        except:
            pass
        
        print("✅ 程序清理完成")

if __name__ == "__main__":
    main()
