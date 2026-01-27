import base64
import subprocess
import time
import re
from pathlib import Path
from typing import Optional, Dict
from loguru import logger

class ProxyManager:
    """Управляет внешними прокси-клиентами (например, Shadowsocks ss-local)."""
    
    def __init__(self, local_port: int = 1080):
        self.local_port = local_port
        self.process: Optional[subprocess.Popen] = None
        self._current_url: Optional[str] = None

    def parse_ss_url(self, url: str) -> Optional[Dict[str, str]]:
        """Парсит ss:// ссылку в компоненты для ss-local."""
        try:
            # Убираем тег/название после #
            clean_url = url.split('#')[0]
            if not clean_url.startswith('ss://'):
                return None
            
            payload = clean_url[5:]
            
            # Стандартный формат: ss://BASE64(method:password)@host:port
            if '@' in payload:
                user_info_b64, server_info = payload.split('@', 1)
                # Добавляем padding для base64 если нужно
                user_info_b64 += '=' * (-len(user_info_b64) % 4)
                user_info = base64.b64decode(user_info_b64).decode('utf-8')
                method, password = user_info.split(':', 1)
                host, port = server_info.split(':', 1)
            else:
                # Альтернативный формат: ss://BASE64(method:password@host:port)
                payload += '=' * (-len(payload) % 4)
                decoded = base64.b64decode(payload).decode('utf-8')
                # method:password@host:port
                match = re.match(r'(.+):(.+)@(.+):(\d+)', decoded)
                if not match: return None
                method, password, host, port = match.groups()

            return {
                "server": host,
                "server_port": port,
                "method": method,
                "password": password,
                "local_port": str(self.local_port)
            }
        except Exception as e:
            logger.error(f"Ошибка парсинга ShadowSocks URL: {e}")
            return None

    def start_shadowsocks(self, ss_url: str) -> bool:
        """Запускает клиент ss-local для создания SOCKS5 туннеля."""
        if self._current_url == ss_url and self.process and self.process.poll() is None:
            return True

        self.stop()
        
        config = self.parse_ss_url(ss_url)
        if not config:
            logger.error("Не удалось разобрать конфигурацию ShadowSocks.")
            return False

        logger.info(f"Запуск ShadowSocks туннеля через {config['server']}:{config['server_port']}...")
        
        try:
            # Команда для ss-local (shadowsocks-libev)
            cmd = [
                "ss-local",
                "-s", config["server"],
                "-p", config["server_port"],
                "-m", config["method"],
                "-k", config["password"],
                "-l", config["local_port"],
                "-b", "127.0.0.1",
                "-u" # Включаем UDP (опционально)
            ]
            
            # В Docker среде ss-local должен быть установлен через apt-get install shadowsocks-libev
            self.process = subprocess.Popen(
                cmd, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE,
                text=True
            )
            
            # Даем время на запуск
            time.sleep(2)
            if self.process.poll() is not None:
                _, err = self.process.communicate()
                logger.error(f"Ошибка запуска ss-local: {err}")
                return False
                
            self._current_url = ss_url
            logger.success(f"ShadowSocks туннель поднят на 127.0.0.1:{self.local_port}")
            return True
            
        except FileNotFoundError:
            logger.error("Утилита 'ss-local' не найдена. Убедитесь, что shadowsocks-libev установлен.")
            return False
        except Exception as e:
            logger.error(f"Критическая ошибка при запуске прокси: {e}")
            return False

    def stop(self):
        """Останавливает процесс прокси."""
        if self.process:
            logger.info("Остановка ShadowSocks туннеля...")
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None
            self._current_url = None

# Глобальный экземпляр для управления
proxy_manager_instance = ProxyManager()
