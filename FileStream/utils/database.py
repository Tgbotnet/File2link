import pymongo
import time
import motor.motor_asyncio
import datetime
from bson.objectid import ObjectId
from bson.errors import InvalidId
from FileStream.server.exceptions import FIleNotFound
from FileStream.config import Links

class Database:
    def __init__(self, uri, database_name):
        self._client = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self.db = self._client[database_name]
        self.col = self.db.users
        self.black = self.db.blacklist
        self.file = self.db.file
        self.stats = self.db.stats_bandwidth

#---------------------[ NEW USER ]---------------------#
    def new_user(self, id):
        return dict(
            id=id,
            join_date=time.time(),
            Links=0
        )

# ---------------------[ ADD USER ]---------------------#
    async def add_user(self, id):
        user = self.new_user(id)
        await self.col.insert_one(user)

# ---------------------[ GET USER ]---------------------#
    async def get_user(self, id):
        user = await self.col.find_one({'id': int(id)})
        return user

# ---------------------[ CHECK USER ]---------------------#
    async def total_users_count(self):
        count = await self.col.count_documents({})
        return count

    async def get_all_users(self):
        all_users = self.col.find({})
        return all_users

# ---------------------[ REMOVE USER ]---------------------#
    async def delete_user(self, user_id):
        await self.col.delete_many({'id': int(user_id)})

# ---------------------[ BAN, UNBAN USER ]---------------------#
    def black_user(self, id):
        return dict(
            id=id,
            ban_date=time.time()
        )

    async def ban_user(self, id):
        user = self.black_user(id)
        await self.black.insert_one(user)

    async def unban_user(self, id):
        await self.black.delete_one({'id': int(id)})

    async def is_user_banned(self, id):
        user = await self.black.find_one({'id': int(id)})
        return True if user else False

    async def total_banned_users_count(self):
        count = await self.black.count_documents({})
        return count
        
# ---------------------[ ADD FILE TO DB ]---------------------#
    async def add_file(self, file_info):
        now = time.time()
        file_info["time"] = now
        if Links.ENABLED:
            file_info["expires_at"] = now + Links.EXPIRY_SECONDS
        fetch_old = await self.get_file_by_fileuniqueid(file_info["user_id"], file_info["file_unique_id"])
        if fetch_old:
            # Re-uploading the same file restarts its window rather than handing
            # back a link that is about to expire.
            if Links.ENABLED:
                await self.set_expiry(fetch_old["_id"], now + Links.EXPIRY_SECONDS)
            return fetch_old["_id"]
        await self.count_links(file_info["user_id"], "+")
        return (await self.file.insert_one(file_info)).inserted_id

# ---------------------[ FIND FILE IN DB ]---------------------#
    async def find_files(self, user_id, range):
        user_files=self.file.find({"user_id": user_id})
        user_files.skip(range[0] - 1)
        user_files.limit(range[1] - range[0] + 1)
        user_files.sort('_id', pymongo.DESCENDING)
        total_files = await self.file.count_documents({"user_id": user_id})
        return user_files, total_files

    async def get_file(self, _id):
        try:
            file_info=await self.file.find_one({"_id": ObjectId(_id)})
            if not file_info:
                raise FIleNotFound
            return file_info
        except InvalidId:
            raise FIleNotFound
    
    async def get_file_by_fileuniqueid(self, id, file_unique_id, many=False):
        if many:
            return self.file.find({"file_unique_id": file_unique_id})
        else:
            file_info=await self.file.find_one({"user_id": id, "file_unique_id": file_unique_id})
        if file_info:
            return file_info
        return False

# ---------------------[ TOTAL FILES ]---------------------#
    async def total_files(self, id=None):
        if id:
            return await self.file.count_documents({"user_id": id})
        return await self.file.count_documents({})

# ---------------------[ DELETE FILES ]---------------------#
    async def delete_one_file(self, _id):
        await self.file.delete_one({'_id': ObjectId(_id)})

# ---------------------[ UPDATE FILES ]---------------------#
    async def update_file_ids(self, _id, file_ids: dict):
        await self.file.update_one({"_id": ObjectId(_id)}, {"$set": {"file_ids": file_ids}})

# ---------------------[ LINK EXPIRY + MESSAGE TRACKING ]---------------------#
    async def ensure_expiry_index(self):
        """Keep the expiry sweep cheap as the collection grows."""
        try:
            await self.file.create_index("expires_at")
        except Exception:
            pass

    async def set_expiry(self, _id, expires_at):
        await self.file.update_one({"_id": ObjectId(_id)}, {"$set": {"expires_at": expires_at}})

    async def get_expired_files(self, now, fallback_seconds, limit=200):
        """Links past their deadline.

        The second clause catches records created before expiry existed, which
        have no expires_at field and are dated purely by upload time.
        """
        query = {"$or": [
            {"expires_at": {"$lte": now}},
            {"expires_at": {"$exists": False}, "time": {"$lte": now - fallback_seconds}},
        ]}
        return await self.file.find(query).limit(limit).to_list(length=limit)

    async def add_log_msg_ids(self, _id, msg_ids):
        """Remember what we posted to the log channel so a purge can wipe it."""
        ids = [int(m) for m in msg_ids if m]
        if not ids:
            return
        await self.file.update_one(
            {"_id": ObjectId(_id)},
            {"$addToSet": {"log_msg_ids": {"$each": ids}}},
        )

    async def add_user_msg_ids(self, _id, chat_id, msg_ids):
        """Remember the uploader's own messages so a purge can clear their chat."""
        ids = [int(m) for m in msg_ids if m]
        if not ids:
            return
        await self.file.update_one(
            {"_id": ObjectId(_id)},
            {"$set": {"user_chat_id": int(chat_id)},
             "$addToSet": {"user_msg_ids": {"$each": ids}}},
        )

# ---------------------[ PAID SYS ]---------------------#
#     async def link_available(self, id):
#         user = await self.col.find_one({"id": id})
#         if user.get("Plan") == "Plus":
#             return "Plus"
#         elif user.get("Plan") == "Free":
#             files = await self.file.count_documents({"user_id": id})
#             if files < 11:
#                 return True
#             return False
        
    async def count_links(self, id, operation: str):
        if operation == "-":
            await self.col.update_one({"id": id}, {"$inc": {"Links": -1}})
        elif operation == "+":
            await self.col.update_one({"id": id}, {"$inc": {"Links": 1}})

    async def update_bandwidth(self, file_id, user_id, bytes_sent):
        today = datetime.date.today().isoformat()
        await self.stats.update_one({"_id": today}, {"$inc": {"bandwidth": bytes_sent}}, upsert=True)
        
        if user_id:
            await self.col.update_one({"id": int(user_id)}, {"$inc": {"bandwidth_used": bytes_sent}})
        await self.file.update_one({"_id": ObjectId(file_id)}, {"$inc": {"bandwidth_used": bytes_sent}})

    async def inc_download_count(self, file_id):
        await self.file.update_one({"_id": ObjectId(file_id)}, {"$inc": {"downloads": 1}})

    async def report_file(self, file_id):
        await self.file.update_one({"_id": ObjectId(file_id)}, {"$inc": {"reports": 1}})

    async def get_reported_files(self, limit=50):
        # Fetch files heavily sorting by 'reports' count natively.
        return await self.file.find({"reports": {"$gt": 0}}).sort("reports", pymongo.DESCENDING).limit(limit).to_list(length=limit)

    async def get_top_users_by_bandwidth(self, limit=50):
        return await self.col.find({"bandwidth_used": {"$exists": True}}).sort("bandwidth_used", pymongo.DESCENDING).limit(limit).to_list(length=limit)

    async def get_top_files_by_bandwidth(self, limit=50):
        return await self.file.find({"bandwidth_used": {"$exists": True}}).sort("bandwidth_used", pymongo.DESCENDING).limit(limit).to_list(length=limit)
    
    async def get_total_bandwidth(self):
        result = await self.stats.aggregate([
            {"$group": {"_id": None, "total": {"$sum": "$bandwidth"}}}
        ]).to_list(1)
        return result[0]["total"] if result else 0

    async def get_bandwidth_stats(self):
        today = datetime.date.today()
        daily = today.isoformat()
        week_dates = [(today - datetime.timedelta(days=i)).isoformat() for i in range(7)]
        month_dates = [(today - datetime.timedelta(days=i)).isoformat() for i in range(30)]
        
        records = await self.stats.find({"_id": {"$in": month_dates}}).to_list(length=30)
        
        bw_daily = 0
        bw_weekly = 0
        bw_monthly = 0
        
        for r in records:
            b = r.get("bandwidth", 0)
            if r["_id"] == daily:
                bw_daily += b
            if r["_id"] in week_dates:
                bw_weekly += b
            bw_monthly += b
            
        return bw_daily, bw_weekly, bw_monthly