# 替换 Nominatim 为高德逆地理编码 API，解决超时问题
import sys
import requests
import json
import time
from datetime import datetime
import pytz
from unittest.mock import Mock

try:
    from rich import print
except Exception:
    pass
from generator import Generator
from stravalib.client import Client
from stravalib.exc import RateLimitExceeded

# ===================== 高德 API 配置（核心修改）=====================
# 替换为你自己的高德 Web 服务 API Key
AMAP_API_KEY = "f32107837ead6cc930a9ea898de2844c"
# 高德逆地理编码 API 地址（无需修改）
AMAP_REVERSE_GEO_URL = "https://restapi.amap.com/v3/geocode/regeo"

def amap_reverse_geocode(lat, lon):
    """
    高德逆地理编码：经纬度转真实地址（替代 Nominatim）
    :param lat: 纬度
    :param lon: 经度
    :return: 格式化地址（如「北京市朝阳区XX街道」），失败返回「中国」
    """
    try:
        # 高德 API 参数（WGS84 坐标需指定 coordtype）
        params = {
            "location": f"{lon},{lat}",  # 高德格式：经度,纬度
            "key": AMAP_API_KEY,
            "coordtype": "wgs84ll",      # 声明输入为 GPS 原始坐标（WGS84）
            "extensions": "base",        # 仅返回基础地址，精简数据
            "batch": "false"
        }
        # 发送请求（设置5秒超时，避免卡壳）
        response = requests.get(AMAP_REVERSE_GEO_URL, params=params, timeout=5)
        response.raise_for_status()  # 抛出 HTTP 错误（如403/500）
        result = response.json()
        
        # 解析高德返回结果
        if result.get("status") == "1" and "regeocode" in result:
            # 优先返回格式化地址，无则返回「中国」
            return result["regeocode"].get("formatted_address", "中国")
        else:
            print(f"高德API返回异常: {result.get('info', '未知错误')}")
            return "中国"
    except Exception as e:
        # 捕获所有异常（网络超时、Key错误等），兜底返回「中国」
        print(f"高德逆地理编码失败(lat={lat}, lon={lon}): {str(e)}")
        return "中国"

# ===================== Mock geopy 并关联高德 API（核心修改）=====================
class MockGeoLocator:
    def reverse(self, location, *args, **kwargs):
        """
        重写 reverse 方法：调用高德 API 替代 Nominatim
        :param location: 元组 (纬度, 经度)
        """
        lat, lon = location  # 解析传入的经纬度
        address = amap_reverse_geocode(lat, lon)  # 调用高德 API
        # 构造和原 Nominatim 一致的返回格式，保证原有代码兼容
        mock = Mock()
        mock.address = address
        return mock

# 强制 Mock geopy，让原有代码调用高德 API
sys.modules['geopy'] = Mock()
sys.modules['geopy.geocoders'] = Mock()
sys.modules['geopy.geocoders.Nominatim'] = MockGeoLocator

# ===================== 原有业务逻辑（无需修改）=====================
def adjust_time(time, tz_name):
    tc_offset = datetime.now(pytz.timezone(tz_name)).utcoffset()
    return time + tc_offset

def adjust_time_to_utc(time, tz_name):
    tc_offset = datetime.now(pytz.timezone(tz_name)).utcoffset()
    return time - tc_offset

def adjust_timestamp_to_utc(timestamp, tz_name):
    tc_offset = datetime.now(pytz.timezone(tz_name)).utcoffset()
    delta = int(tc_offset.total_seconds())
    return int(timestamp) - delta

def to_date(ts):
    """Parse ISO format timestamp string to datetime object."""
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        ts_fmts = ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"]
        for ts_fmt in ts_fmts:
            try:
                return datetime.strptime(ts, ts_fmt)
            except ValueError:
                pass
        raise ValueError(f"cannot parse timestamp {ts} into date")

def make_activities_file(
    sql_file, data_dir, json_file, file_suffix="gpx", activity_title_dict={}
):
    generator = Generator(sql_file)
    generator.sync_from_data_dir(
        data_dir, file_suffix=file_suffix, activity_title_dict=activity_title_dict
    )
    activities_list = generator.load()
    with open(json_file, "w") as f:
        json.dump(activities_list, f)

def make_strava_client(client_id, client_secret, refresh_token):
    client = Client()
    refresh_response = client.refresh_access_token(
        client_id=client_id, client_secret=client_secret, refresh_token=refresh_token
    )
    client.access_token = refresh_response["access_token"]
    return client

def get_strava_last_time(client, is_milliseconds=True):
    """Get last run time from Strava, return 0 if exception."""
    try:
        activity = None
        activities = client.get_activities(limit=10)
        activities = list(activities)
        activities.sort(key=lambda x: x.start_date, reverse=True)
        for a in activities:
            if a.type == "Run":
                activity = a
                break
        else:
            return 0
        end_date = activity.start_date + activity.elapsed_time
        last_time = int(datetime.timestamp(end_date))
        if is_milliseconds:
            last_time = last_time * 1000
        return last_time
    except Exception as e:
        print(f"Something wrong to get last time err: {str(e)}")
        return 0

def upload_file_to_strava(client, file_name, data_type, force_to_run=True):
    with open(file_name, "rb") as f:
        try:
            if force_to_run:
                r = client.upload_activity(
                    activity_file=f, data_type=data_type, activity_type="run"
                )
            else:
                r = client.upload_activity(activity_file=f, data_type=data_type)
        except RateLimitExceeded as e:
            timeout = e.timeout
            print(f"Strava API Rate Limit Exceeded. Retry after {timeout} seconds")
            time.sleep(timeout)
            if force_to_run:
                r = client.upload_activity(
                    activity_file=f, data_type=data_type, activity_type="run"
                )
            else:
                r = client.upload_activity(activity_file=f, data_type=data_type)
        print(
            f"Uploading {data_type} file: {file_name} to strava, upload_id: {r.upload_id}."
        )
