"use strict";
const fs=require('node:fs');const path=require('node:path');const util=require('node:util');
const LEVELS={debug:10,info:20,warn:30,error:40,silent:99};
function redact(text){
 if(/收到.*消息|\[master\]|回复.*复读|重写一次：|读取链接：/.test(text))return text.split(/：|→/)[0]+' [私密内容已省略]';
 return String(text).replace(/wxid_[\w-]+|[\w.+-]+@[\w.-]+\.\w+|\d+@chatroom/g,'[身份已脱敏]').replace(/Bearer\s+[^\s"']+|sk-[\w-]{8,}|([?&](?:token|key|secret|signature)=)[^&\s]+/ig,'[凭据已省略]');
}
function rotate(file,maxBytes=5*1024*1024){
 if(!fs.existsSync(file)||fs.statSync(file).size<maxBytes)return;
 for(let n=3;n>=1;n--){const from=n===1?file:file+'.'+(n-1),to=file+'.'+n;if(fs.existsSync(from))fs.renameSync(from,to);}
}
function createLogger({level='info',file=null}={}){
 if(file)fs.mkdirSync(path.dirname(file),{recursive:true});
 function emit(kind,args){
  if(LEVELS[kind]<(LEVELS[level]??20))return;
  const ts=new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',hour12:false,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(new Date());
  const text=redact(args.map(a=>typeof a==='string'?a:util.inspect(a,{depth:2})).join(' '));const line=`[${ts}] ${kind.toUpperCase()} ${text}`;
  try{if(process.stdout.isTTY||process.env.WHALE_CONSOLE_LOG==='1')console.log(line);}catch{}
  if(file)try{rotate(file);fs.appendFileSync(file,line+'\n','utf8');}catch{}
 }
 return {debug:(...a)=>emit('debug',a),info:(...a)=>emit('info',a),warn:(...a)=>emit('warn',a),error:(...a)=>emit('error',a),level,close(){}};
}
module.exports={createLogger,LEVELS,redact,rotate};
