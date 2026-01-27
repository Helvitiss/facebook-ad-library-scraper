import os
import json
import shutil
import pytest
from pathlib import Path
from src.core.config import Config

@pytest.fixture
def temp_config_dir():
    test_dir = Path("tests/temp_pytest_config")
    if test_dir.exists():
        shutil.rmtree(test_dir)
    test_dir.mkdir(parents=True)
    yield test_dir
    # Очистка после теста
    if test_dir.exists():
        shutil.rmtree(test_dir)

def test_config_load_and_save(temp_config_dir):
    config_path = temp_config_dir / "config.json"
    env_path = temp_config_dir / ".env"
    
    # 1. Начальный конфиг
    initial_cfg = {
        "scraper": {"concurrent_requests": 10, "proxy_url": "old_proxy"},
        "exporter": {"min_reaches": 100},
        "telegram": {"token": "old_token", "user_ids": [123]},
        "facebook_api": {"doc_ids": {"a": "1"}},
        "video_extensions": [".mp4"]
    }
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(initial_cfg, f)
    
    # Эмуляция ENV
    os.environ["TG_TOKEN"] = "env_token"
    os.environ["TG_USER_IDS"] = "444,555"
    
    config = Config(config_path)
    
    # Проверка загрузки
    assert config.data.telegram.token == "env_token"
    assert config.data.telegram.user_ids == [444, 555]
    
    # 2. Сохранение обычного параметра
    new_data = config.data.model_dump()
    new_data["exporter"]["min_reaches"] = 777
    config.save(new_data)
    
    with open(config_path, "r", encoding="utf-8") as f:
        saved_json = json.load(f)
    assert saved_json["exporter"]["min_reaches"] == 777
    
    # 3. Сохранение секретного параметра
    new_data["telegram"]["token"] = "SECRET_TOKEN"
    config.save(new_data)
    
    with open(env_path, "r", encoding="utf-8") as f:
        env_content = f.read()
    assert "TG_TOKEN=SECRET_TOKEN" in env_content
    assert os.environ["TG_TOKEN"] == "SECRET_TOKEN"
    
    # Проверка маскировки в JSON
    with open(config_path, "r", encoding="utf-8") as f:
        saved_json = json.load(f)
    assert saved_json["telegram"]["token"] == "ENV_VAR"

@pytest.mark.asyncio
async def test_config_reload_affects_runtime():
    # Проверка того, что изменения применяются без перезапуска (через os.environ)
    # Этот тест скорее проверяет механизм _update_env_file
    from src.core.config import config_instance
    
    old_val = os.getenv("PROXY_URL", "")
    try:
        cfg_dict = config_instance.data.model_dump()
        cfg_dict["scraper"]["proxy_url"] = "http://username:password@ip:port"
        config_instance.save(cfg_dict)
        
        assert os.environ["PROXY_URL"] == "http://username:password@ip:port"
        assert config_instance.data.scraper.proxy_url == "http://username:password@ip:port"
    finally:
        # Возвращаем как было
        if old_val:
            os.environ["PROXY_URL"] = old_val
        else:
            os.environ.pop("PROXY_URL", None)
