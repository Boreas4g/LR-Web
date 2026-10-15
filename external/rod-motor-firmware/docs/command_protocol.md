# 杆电机命令协议

所有命令都是一行一个 JSON，串口结尾带换行。

返回编号统一使用 `T * 10 + 1`，例如 `101 -> 1011`。

| T | 请求 | 返回字段 |
|---:|---|---|
| 101 | `{"T":101}` | `ok,id,pos,rad,status,speed,load,mode` |
| 102 | `{"T":102,"pos":2047,"spd":120,"acc":10}` | `ok,id,pos,rad,spd,acc` |
| 103 | `{"T":103}` | `ok,id,rad,angle,pos,status,speed,load,voltage,current,temper,mode` |
| 104 | `{"T":104,"rad":1.57,"spd":120,"acc":10}` | `ok,id,rad,angle,pos,spd,acc` |
| 105 | `{"T":105,"p":16,"i":0,"d":0}` | `ok,id,p,i,d` |
| 106 | `{"T":106}` | `ok,id,p,i,d` |
| 107 | `{"T":107,"raw":16,"new":17}` | `ok,old,new,currentId,newOnline` |
| 108 | `{"T":108}` | `ok,id` |

说明：

`id` 在 `101-106` 和 `108` 中可选，默认使用当前运行时杆电机 ID。  
`T=107` 可以用 `raw`、`old` 或 `id` 表示当前 ID。
