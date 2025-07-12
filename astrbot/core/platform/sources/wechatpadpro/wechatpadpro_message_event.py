import asyncio
import base64
import io
from typing import TYPE_CHECKING

import aiohttp
import cv2
from PIL import Image as PILImage  # 使用别名避免冲突

from astrbot import logger
from astrbot.core.message.components import (
    Image,
    Plain,
    WechatEmoji,
    Record,
    Video,
    Xml,
)  # Import Image
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.utils.tencent_record_helper import audio_to_tencent_silk_base64

if TYPE_CHECKING:
    from .wechatpadpro_adapter import WeChatPadProAdapter


class WeChatPadProMessageEvent(AstrMessageEvent):
    def __init__(
        self,
        message_str: str,
        message_obj: AstrBotMessage,
        platform_meta: PlatformMetadata,
        session_id: str,
        adapter: "WeChatPadProAdapter",  # 传递适配器实例
    ):
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.message_obj = message_obj  # Save the full message object
        self.adapter = adapter  # Save the adapter instance

    async def send_streaming(
        self, generator, use_fallback: bool = False
    ):
        buffer = None
        async for chain in generator:
            if not buffer:
                buffer = chain
            else:
                buffer.chain.extend(chain.chain)
        
        if not buffer:
            return

        buffer.squash_plain() 
        
        await self.send(buffer)
        # 注意：最后调用 super().send_streaming 是为了正确处理统计等基类逻辑
        return await super().send_streaming(generator, use_fallback)

    async def send(self, message: MessageChain):
        async with aiohttp.ClientSession() as session:
            for comp in message.chain:
                await asyncio.sleep(1)
                if isinstance(comp, Plain):
                    await self._send_text(session, comp.text)
                elif isinstance(comp, Image):
                    await self._send_image(session, comp)
                elif isinstance(comp, WechatEmoji):
                    await self._send_emoji(session, comp)
                elif isinstance(comp, Record):
                    await self._send_voice(session, comp)
                elif isinstance(comp, Video):
                    await self._send_video(session, comp)
        await super().send(message)

    async def _send_image(self, session: aiohttp.ClientSession, comp: Image):
        b64 = await comp.convert_to_base64()
        raw = self._validate_base64(b64)
        b64c = self._compress_image(raw)
        payload = {
            "MsgItem": [
                {"ImageContent": b64c, "MsgType": 3, "ToUserName": self.session_id}
            ]
        }
        url = f"{self.adapter.base_url}/message/SendImageNewMessage"
        await self._post(session, url, payload)

    async def _send_text(self, session: aiohttp.ClientSession, text: str):
        if (
            self.message_obj.type == MessageType.GROUP_MESSAGE  # 确保是群聊消息
            and self.adapter.settings.get(
                "reply_with_mention", False
            )  # 检查适配器设置是否启用 reply_with_mention
            and self.message_obj.sender  # 确保有发送者信息
            and (
                self.message_obj.sender.user_id or self.message_obj.sender.nickname
            )  # 确保发送者有 ID 或昵称
        ):
            # 优先使用 nickname，如果没有则使用 user_id
            mention_text = (
                self.message_obj.sender.nickname or self.message_obj.sender.user_id
            )
            message_text = f"@{mention_text} {text}"
            # logger.info(f"已添加 @ 信息: {message_text}")
        else:
            message_text = text
        if self.get_group_id() and "#" in self.session_id:
            session_id = self.session_id.split("#")[0]
        else:
            session_id = self.session_id
        payload = {
            "MsgItem": [
                {
                    "MsgType": 1,
                    "TextContent": message_text,
                    "ToUserName": session_id,
                }
            ]
        }
        url = f"{self.adapter.base_url}/message/SendTextMessage"
        await self._post(session, url, payload)

    async def _send_emoji(self, session: aiohttp.ClientSession, comp: WechatEmoji):
        payload = {
            "EmojiList": [
                {
                    "EmojiMd5": comp.md5,
                    "EmojiSize": comp.md5_len,
                    "ToUserName": self.session_id,
                }
            ]
        }
        url = f"{self.adapter.base_url}/message/SendEmojiMessage"
        await self._post(session, url, payload)

    async def _send_voice(self, session: aiohttp.ClientSession, comp: Record):
        record_path = await comp.convert_to_file_path()
        # 默认已经存在 data/temp 中
        b64, duration = await audio_to_tencent_silk_base64(record_path)
        payload = {
            "ToUserName": self.session_id,
            "VoiceData": b64,
            "VoiceFormat": 4,
            "VoiceSecond": duration,
        }
        url = f"{self.adapter.base_url}/message/SendVoice"
        await self._post(session, url, payload)

    async def _send_video(self, session: aiohttp.ClientSession, comp: Video):
        try:
            video_path = await comp.convert_to_file_path()

            # 一次性获取缩略图和时长
            generated_thumb_b64, play_length = self._get_video_thumbnail_and_duration(
                video_path
            )

            # 优先使用组件自带的封面
            thumbnail_b64 = comp.cover or generated_thumb_b64
            if not thumbnail_b64:
                logger.error(f"视频 {video_path} 无法生成缩略图，发送失败。")
                return

            with open(video_path, "rb") as f:
                video_bytes = f.read()
            video_data_array = list(video_bytes)

            # --- Step 1: 上传视频到 CDN ---
            upload_payload = {
                "ToUserName": self.session_id,
                "ThumbData": thumbnail_b64,
                "VideoData": video_data_array,
            }
            upload_url = f"{self.adapter.base_url}/message/CdnUploadVideo"
            upload_result = await self._post(session, upload_url, upload_payload)

            # --- Step 2: 使用 ForwardVideoMessage 发送已上传的视频 ---
            if upload_result and upload_result.get("Code") == 200:
                data = upload_result.get("Data", {})

                forward_payload = {
                    "ForwardVideoList": [
                        {
                            "ToUserName": self.session_id,
                            "AesKey": data.get("FileAesKey", ""),
                            "CdnThumbLength": data.get("ThumbDataSize", 0),
                            "CdnVideoUrl": data.get("FileID", ""),
                            "Length": data.get("VideoDataSize", 0),
                            "PlayLength": play_length,  # 使用动态获取的时长
                        }
                    ]
                }

                send_url = f"{self.adapter.base_url}/message/ForwardVideoMessage"
                await self._post(session, send_url, forward_payload)
            else:
                logger.error(f"CdnUploadVideo 失败，无法转发视频。响应: {upload_result}")

        except Exception as e:
            logger.error(f"发送视频过程中出现异常: {e}")

    @staticmethod
    def _get_video_thumbnail_and_duration(video_path: str) -> tuple[str, int]:
        """从视频文件高效地获取第一帧缩略图和视频时长。"""
        try:
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                logger.error(f"无法打开视频文件: {video_path}")
                return "", 0

            # 获取时长
            frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            fps = cap.get(cv2.CAP_PROP_FPS)
            duration = round(frame_count / fps) if fps > 0 else 0

            # 获取缩略图
            success, frame = cap.read()
            cap.release()

            if not success:
                logger.error(f"无法从视频文件中读取帧: {video_path}")
                return "", duration  # 即使没读到帧，时长可能还是对的

            # 将帧编码为JPEG格式的字节流
            success, buffer = cv2.imencode(".jpg", frame)
            if not success:
                logger.error(f"无法将帧编码为JPEG: {video_path}")
                return "", duration

            thumbnail_b64 = base64.b64encode(buffer).decode("utf-8")
            return thumbnail_b64, duration

        except Exception as e:
            logger.error(f"使用OpenCV处理视频时出现异常: {video_path}, 错误: {e}")
            return "", 0

    async def _send_app_msg(self, session: aiohttp.ClientSession, comp: Xml):
        """发送App消息（如XML卡片）- 此方法当前未被视频流程使用，仅作为重构保留"""
        app_msg_payload = {
            "AppList": [
                {
                    "ToUserName": self.session_id,
                    "ContentXML": comp.data,
                    "ContentType": 49,
                }
            ]
        }
        send_url = f"{self.adapter.base_url}/message/SendAppMessage"
        await self._post(session, send_url, app_msg_payload)

    @staticmethod
    def _validate_base64(b64: str) -> bytes:
        return base64.b64decode(b64, validate=True)

    @staticmethod
    def _compress_image(data: bytes) -> str:
        img = PILImage.open(io.BytesIO(data))
        buf = io.BytesIO()
        if img.format == "JPEG":
            img.save(buf, "JPEG", quality=80)
        else:
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.save(buf, "JPEG", quality=80)
        # logger.info("图片处理完成！！！")
        return base64.b64encode(buf.getvalue()).decode()

    async def _post(self, session, url, payload):
        params = {"key": self.adapter.auth_key}
        try:
            async with session.post(url, params=params, json=payload) as resp:
                data = await resp.json()
                if resp.status != 200 or data.get("Code") != 200:
                    logger.error(f"{url} failed: {resp.status} {data}")
                return data
        except Exception as e:
            logger.error(f"{url} error: {e}")
            return None


# TODO: 添加对其他消息组件类型的处理 (Record, Video, At等)
# elif isinstance(component, Record):
#     pass
# elif isinstance(component, Video):
#     pass
# elif isinstance(component, At):
#     pass
# ...
