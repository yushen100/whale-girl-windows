"use strict";
const fs=require('node:fs');const path=require('node:path');const {createHash}=require('node:crypto');
class DeliveryStore {
 constructor(file){this.file=file;this.rows={};if(file&&fs.existsSync(file)){this.rows=JSON.parse(fs.readFileSync(file,'utf8'));if(!this.rows||Array.isArray(this.rows)||typeof this.rows!=='object')throw Error('投递记录损坏，拒绝覆盖');}}
 id(platform,chat,msg){return createHash('sha256').update([platform,chat,msg].join('\0')).digest('hex');}
 save(){if(!this.file)return;fs.mkdirSync(path.dirname(this.file),{recursive:true});const temp=this.file+'.tmp';let fd=fs.openSync(temp,'w');try{fs.writeFileSync(fd,JSON.stringify(this.rows),'utf8');fs.fsyncSync(fd);}finally{fs.closeSync(fd);}fs.renameSync(temp,this.file);}
 put(id,value){for(const [key,row] of Object.entries(this.rows)){if(['confirmed','ignored'].includes(row.status)&&Date.now()-(row.confirmedAt||row.createdAt)>7*86400000)delete this.rows[key];}const previous=this.rows[id];if(!previous&&this.pending().length>=1000)throw Error('待投递任务过多，请检查桥接');this.rows[id]={...value,id,createdAt:previous?.createdAt||Date.now(),status:value.status||'pending'};try{this.save();}catch(e){if(previous)this.rows[id]=previous;else delete this.rows[id];throw e;}return this.rows[id];}
 get(id){return this.rows[id];}
 ack(id,confirmed){const previous=this.rows[id];if(!previous)return null;if(previous.status==='confirmed')return previous;const row={...previous};if(confirmed){row.status='confirmed';row.confirmedAt=Date.now();}else row.lastFailureAt=Date.now();this.rows[id]=row;try{this.save();}catch(error){this.rows[id]=previous;throw error;}return row;}
 pending(){return Object.values(this.rows).filter(r=>r.status==='pending'||r.status==='generating');}
}
module.exports={DeliveryStore};
