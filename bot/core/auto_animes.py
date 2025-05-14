from asyncio import gather, create_task, sleep as asleep, Event
from os import path as ospath
from aiofiles.os import remove as aioremove
from traceback import format_exc
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram import filters

from bot import bot, bot_loop, Var, ani_cache, ffQueue, ffLock, ff_queued
from .database import db
from .func_utils import encode, editMessage, sendMessage, convertBytes
from .text_utils import TextEditor
from .ffencoder import FFEncoder
from .tguploader import TgUploader
from .reporter import rep

btn_formatter = {
    '1080': '𝟭𝟬𝟴𝟬𝗽',
    '720': '𝟳𝟮𝟬𝗽',
    '480': '𝟰𝟴𝟬𝗽',
    '360': '𝟯𝟲𝟬𝗽'
}

# Handler for private messages containing files
@bot.on_message(filters.private & filters.document)
async def handle_private_message(client, message):
    try:
        # Check if the message contains a file
        if message.document:
            file_name = message.document.file_name
            await rep.report(f"New File Received from {message.from_user.mention}\n\nFile: {file_name}", "info")

            # Download the file
            download_path = f"./downloads/{file_name}"
            await message.download(file_name=download_path)

            # Process the file (encode and upload)
            await get_animes(file_name, download_path)
    except Exception as error:
        await rep.report(f"Error in handle_private_message: {format_exc()}", "error")

async def get_animes(name, file_path, force=False):
    try:
        aniInfo = TextEditor(name)
        await aniInfo.load_anilist()
        ani_id, ep_no = aniInfo.adata.get('id'), aniInfo.pdata.get("episode_number")

        if ani_id not in ani_cache['ongoing']:
            ani_cache['ongoing'].add(ani_id)
        elif not force:
            return

        if not force and ani_id in ani_cache['completed']:
            return

        if force or (not (ani_data := await db.getAnime(ani_id)) \
                or (ani_data and not (qual_data := ani_data.get(ep_no))) \
                or (ani_data and qual_data and not all(qual for qual in qual_data.values()))):

            if "[Batch]" in name:
                await rep.report(f"File Skipped!\n\n{name}", "warning")
                return

            await rep.report(f"New Anime File Found!\n\n{name}", "info")
            post_msg = await bot.send_photo(
                Var.MAIN_CHANNEL,
                photo=await aniInfo.get_poster(),
                caption=await aniInfo.get_caption()
            )

            await asleep(1.5)
            stat_msg = await sendMessage(Var.MAIN_CHANNEL, f"‣ <b>Anime Name :</b> <b><i>{name}</i></b>\n\n<i>Processing...</i>")

            if not ospath.exists(file_path):
                await rep.report(f"File Download Incomplete, Try Again", "error")
                await stat_msg.delete()
                return

            post_id = post_msg.id
            ffEvent = Event()
            ff_queued[post_id] = ffEvent
            if ffLock.locked():
                await editMessage(stat_msg, f"‣ <b>Anime Name :</b> <b><i>{name}</i></b>\n\n<i>Queued to Encode...</i>")
                await rep.report("Added Task to Queue...", "info")
            await ffQueue.put(post_id)
            await ffEvent.wait()

            await ffLock.acquire()
            btns = []
            for qual in Var.QUALS:
                filename = await aniInfo.get_upname(qual)
                await editMessage(stat_msg, f"‣ <b>Anime Name :</b> <b><i>{name}</i></b>\n\n<i>Ready to Encode...</i>")

                await asleep(1.5)
                await rep.report("Starting Encode...", "info")
                try:
                    out_path = await FFEncoder(stat_msg, file_path, filename, qual).start_encode()
                except Exception as e:
                    await rep.report(f"Error: {e}, Cancelled, Retry Again !", "error")
                    await stat_msg.delete()
                    ffLock.release()
                    return
                await rep.report("Successfully Compressed Now Going To Upload...", "info")

                await editMessage(stat_msg, f"‣ <b>Anime Name :</b> <b><i>{filename}</i></b>\n\n<i>Ready to Upload...</i>")
                await asleep(1.5)
                try:
                    msg = await TgUploader(stat_msg).upload(out_path, qual)
                except Exception as e:
                    await rep.report(f"Error: {e}, Cancelled, Retry Again !", "error")
                    await stat_msg.delete()
                    ffLock.release()
                    return
                await rep.report("Successfully Uploaded File into Tg...", "info")

                msg_id = msg.id
                link = f"https://telegram.me/{(await bot.get_me()).username}?start={await encode('get-' + str(msg_id * abs(Var.FILE_STORE)))}"

                if post_msg:
                    if len(btns) != 0 and len(btns[-1]) == 1:
                        btns[-1].insert(1, InlineKeyboardButton(f"{btn_formatter[qual]} - {convertBytes(msg.document.file_size)}", url=link))
                    else:
                        btns.append([InlineKeyboardButton(f"{btn_formatter[qual]} - {convertBytes(msg.document.file_size)}", url=link)])
                    await editMessage(post_msg, post_msg.caption.html if post_msg.caption else "", InlineKeyboardMarkup(btns))

                await db.saveAnime(ani_id, ep_no, qual, post_id)
                bot_loop.create_task(extra_utils(msg_id, out_path))
            ffLock.release()

            await stat_msg.delete()
            await aioremove(file_path)
        ani_cache['completed'].add(ani_id)
    except Exception as error:
        await rep.report(f"Error in get_animes: {format_exc()}", "error")

async def extra_utils(msg_id, out_path):
    msg = await bot.get_messages(Var.FILE_STORE, message_ids=msg_id)

    if Var.BACKUP_CHANNEL != 0:
        for chat_id in Var.BACKUP_CHANNEL.split():
            await msg.copy(int(chat_id))

    # MediaInfo, ScreenShots, Sample Video ( Add-ons Features )
