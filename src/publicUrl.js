"use strict";
const dns=require('node:dns');const net=require('node:net');
function isPublicAddress(address){
 const ip=String(address).toLowerCase().replace(/^\[|\]$/g,'');
 if(net.isIP(ip)===4){const [a,b,c]=ip.split('.').map(Number);return !(a===0||a===10||a===127||a>=224||a===169&&b===254||a===172&&b>=16&&b<=31||a===192&&(b===168||b===0||b===2)||a===100&&b>=64&&b<=127||a===198&&(b===18||b===19||b===51&&c===100)||a===203&&b===0&&c===113);}
 if(net.isIP(ip)===6)return /^[23]/.test(ip)&&!ip.startsWith('2001:db8:')&&!ip.startsWith('2002:')&&!ip.startsWith('2001:0:')&&!ip.startsWith('2001::');
 return false;
}
async function publicTarget(value,{resolver=dns.promises.lookup,deadline=Date.now()+6000}={}){
 const url=new URL(value);if(!['http:','https:'].includes(url.protocol)||url.username||url.password)throw Error('blocked_url');
 for(const [key,value] of url.searchParams){if(/token|secret|password|authorization|api[_-]?key|^key$|cookie|signature|session|credential/i.test(key)||/[\w.+-]+@[\w.-]+\.\w+|wxid_|1[3-9]\d{9}/i.test(value))throw Error('private_url');}
 const host=url.hostname.replace(/^\[|\]$/g,'');
 const left=Math.max(1,deadline-Date.now());let timer;
 let addresses;
 try{addresses=net.isIP(host)?[{address:host,family:net.isIP(host)}]:await Promise.race([resolver(host,{all:true,verbatim:true}),new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('dns_timeout')),left);})]);}finally{clearTimeout(timer);}
 if(!addresses.length||addresses.some(x=>!isPublicAddress(x.address)))throw Error('blocked_address');
 return {url,address:addresses[0]};
}
module.exports={isPublicAddress,publicTarget};
