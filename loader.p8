pico-8 cartridge // http://www.pico-8.com
version 41
__lua__
-- pico tape loader
-- p8tape loader version 10
-- loads carts from cassette,
-- spectrum style.
-- start it with:
--   python3 p8tape.py play

pk=0x4300  -- decoded packet (285)
rx=0x4500  -- incoming chars (570)
rq=0x4800  -- request byte
have=0
piped=stat(6)=="tape"
eof=false

st="title"  -- title wait tape err go
t=0
tstate=0    -- 0 idle 1 pilot 2 data
            -- 3 error 4 done
gname=nil   -- cart file to load
shown=false
failed=nil
gotime=0
hdr={}
scr=nil
sx,sy,sw,sh=4,4,120,120

function _init()
 make_sfx()
end

-->8
-- link to p8tape.py

-- read one 285-byte packet:
-- 44 bytes of tape data, then
-- up to 240 bytes of tape audio.
-- each byte arrives as two
-- chars "@".."o" (0-15).
function unnib(i)
 poke(pk+i,(@(rx+i*2)-64)*16+@(rx+i*2+1)-64)
end

function read_packet()
 while have<570 do
  local n=flr(serial(0x804,rx+have,570-have))
  if n<=0 then eof=true return false end
  have+=n
 end
 have=0
 for i=0,44 do unnib(i) end
 if @pk!=0xa5 then
  -- out of step: skip a char
  memcpy(rx,rx+1,569)
  have=569
  return false
 end
 for i=45,44+min(@(pk+44),240) do
  unnib(i)
 end
 return true
end

-- play the real tape sound through
-- pico-8's raw audio output
-- (8-bit, 5512 hz)
function play_audio()
 local n=min(@(pk+44),240)
 if n>0 and st!="title" then
  serial(0x808,pk+45,n)
 end
end

-- ask for the next packet
function request()
 poke(rq,0xf7)
 serial(0x805,rq,1)
end

function handle()
 local kind,s=@(pk+1),@(pk+2)
 local n=@(pk+5)
 local off=@(pk+6)+@(pk+7)*256
 tstate=s

 if (st=="title" or st=="wait")
 and (s==1 or s==2) then
  enter_tape()
 end
 if st!="tape" then return end

 if kind==2 and n>1 then
  -- block header: name
  local nm=""
  for i=1,n-1 do
   nm..=chr(@(pk+8+i))
  end
  if @(pk+8)==2 then gname=nm end
  if not shown then
   shown=true
   -- replace "searching..."
   rectfill(sx+2,sy+2,sx+sw-3,sy+7,0)
   print("found: "..sub(split(nm,".",false)[1],1,22),sx+2,sy+2,7)
  end
 elseif kind==1 then
  for i=0,n-1 do
   screen_byte(off+i,@(pk+8+i))
  end
 end

 if s==3 then
  st="err" sfx(3,0)
 elseif s==4 and gname then
  st="go" gotime=20
  sfx(2,0)
 end
end

function enter_tape()
 st="tape"
 cls(0)
 hdr={} scr=nil
 gname=nil shown=false failed=nil
 print("searching...",sx+2,sy+2,7)
end

-->8
-- loading picture

function start_screen()
 local x,y,w,h=hdr[1],hdr[2],hdr[3],hdr[4]
 -- picture formats 3, 4 and 5
 if hdr[0]<3 or hdr[0]>5 then return end
 if w==0 or h==0 or x+w>128 or y+h>128
 or w%8>0 or h%8>0 then return end
 scr={mode=hdr[0],x=x,y=y,w=w,h=h,bpr=w\8,bw=w\2,
  order={},col={},swept=0}
 -- spectrum row order: thirds,
 -- then pixel line, then cell row
 for third=0,(h-1)\64 do
  for line=0,7 do
   for cell=0,7 do
    local r=third*64+cell*8+line
    if r<h then add(scr.order,r) end
   end
  end
 end
end

function screen_byte(k,v)
 if k<5 then
  hdr[k]=v
  if k==4 then start_screen() end
  return
 end
 if not scr then return end
 k-=5
 local s=scr
 if s.mode==5 then pic5(s,k,v) return end
 local nb=s.bpr*s.h
 if k<nb then
  -- black & white pass:
  -- white ink on black paper
  local r=s.order[k\s.bpr+1]
  local x,y=s.x+k%s.bpr*8,s.y+r
  for b=0,7 do
   pset(x+b,y,(v&(128>>b))>0 and 7 or 0)
  end
 elseif s.mode==4 then
  -- colour pass: kept out of sight
  -- until it's all here, then swept
  -- down fast, spectrum style
  local j=k-nb
  s.col[j]=v
  if j==s.w\2*s.h-1 then s.sweep=0 end
 else
  -- older tapes: one 8x8 square
  -- at a time, 32 bytes each
  local j=k-nb
  local cell,within=j\32,j%32
  local cx,cy=cell%s.bpr,cell\s.bpr
  if cy<s.h\8 then
   poke(0x6000+(s.y+cy*8+within\4)*64
    +s.x\2+cx*4+within%4,v)
  end
 end
end

-- one row of the outline:
-- white ink on black paper
function outline(s,r,c,v)
 local x,y=s.x+c*8,s.y+r
 for b=0,7 do
  pset(x+b,y,(v&(128>>b))>0 and 7 or 0)
 end
end

-- one row of real colour
function colour_row(s,r)
 local a=0x6000+(s.y+r)*64+s.x\2
 for c=0,s.bw-1 do
  poke(a+c,s.col[r*s.bw+c] or 0)
 end
end

-- format 5: each outline row comes
-- with a hidden row of colour, so
-- the outline draws slowly all the
-- way through. then the last data
-- sweeps the colour down the screen.
function pic5(s,k,v)
 local chunk=s.bpr+s.bw
 if k<s.h*chunk then
  local i,o=k\chunk,k%chunk
  if o<s.bpr then
   outline(s,s.order[i+1],o,v)
  else
   s.col[i*s.bw+o-s.bpr]=v
  end
 else
  -- sweep: 3 bytes per row
  local upto=min(s.h,(k-s.h*chunk)\3+1)
  while s.swept<upto do
   colour_row(s,s.swept)
   s.swept+=1
  end
 end
end

-->8
-- update & draw

-- colour sweep: 3 rows a frame,
-- top to bottom (~1.5 seconds)
function sweep_step()
 local s=scr
 if not s or not s.sweep then return end
 local bw=s.w\2
 for n=1,3 do
  local r=s.sweep
  if r>=s.h then s.sweep=nil return end
  local a=0x6000+(s.y+r)*64+s.x\2
  for c=0,bw-1 do
   poke(a+c,s.col[r*bw+c] or 0)
  end
  s.sweep=r+1
 end
end

function _update()
 t+=1
 if piped and not eof then
  if read_packet() then
   handle()
   play_audio()
   request()
  end
 end
 sweep_step()

 if st=="title" or st=="err" then
  if piped and (btnp(4) or btnp(5)) then
   -- tell p8tape.py to start a wav
   poke(rq,0xf8)
   serial(0x805,rq,1)
   st="wait" failed=nil
  end
 elseif st=="go" then
  gotime-=1
  if gotime<=0 then
   sfx(-1,0)
   load(gname)
   -- only gets here if it failed
   failed=gname st="err"
  end
 end
end

-- one stripe, around the picture
-- the border is always the same
-- size; it's drawn on top of the
-- picture's edges
function band(y0,y1,c)
 for y=y0,min(y1,127) do
  if y<sy or y>=sy+sh then
   rectfill(0,y,127,y,c)
  else
   if sx>0 then rectfill(0,y,sx-1,y,c) end
   if sx+sw<128 then rectfill(sx+sw,y,127,y,c) end
  end
 end
end

function draw_border(s)
 if s==2 then
  -- data: thin blue/yellow
  local y=0
  while y<128 do
   local h=1+flr(rnd(3))
   band(y,y+h-1,rnd(1)<0.5 and 1 or 10)
   y+=h
  end
 elseif s==1 then
  -- pilot: wide red/cyan
  for y=0,127 do
   band(y,y,((y+t*2)\8)%2==0 and 8 or 12)
  end
 else
  band(0,127,0)
 end
end

function _draw()
 if st=="title" then title() return end
 if st=="wait" then
  cls(0)
  print("searching...",sx+2,sy+2,7)
  print("press play on tape",2,121,7)
  if eof then print("decoder stopped",2,113,8) end
  return
 end
 draw_border(st=="tape" and tstate or 0)
 if st=="err" then
  rectfill(0,119,127,127,0)
  print(failed and "could not open "..failed
   or "r tape loading error, 0:1",2,121,7)
 elseif eof then
  rectfill(0,119,127,127,0)
  print("decoder stopped",2,121,8)
 end
end

function cprint(s,y,c)
 print(s,64-#s*2,y,c)
end

function reel(x,y)
 circfill(x,y,6,6)
 for k=0,2 do
  local a=k/3-t/90
  line(x,y,x+cos(a)*5,y+sin(a)*5,5)
 end
 circfill(x,y,1,0)
end

function title()
 cls(1)
 for i=0,23 do
  pset((i*41+t\3)%128,(i*29)%128,13)
 end
 print("\^w\^tpico tape",29,9,0)
 print("\^w\^tpico tape",28,8,7)
 rectfill(20,30,107,90,5)
 rect(20,30,107,90,0)
 rectfill(26,34,101,74,7)
 rectfill(26,36,101,38,8)
 rectfill(26,39,101,41,9)
 print("pico-8 carts",29,44,0)
 print("c90",89,44,8)
 rectfill(40,52,87,68,0)
 reel(50,60)
 reel(77,60)
 rectfill(36,80,91,90,6)
 circfill(45,85,2,0)
 circfill(82,85,2,0)
 if not piped then
  cprint("start me with",100,8)
  cprint("p8tape.py play",110,7)
 elseif (t\15)%2==0 then
  cprint("press x to load",104,7)
 end
end

-->8
-- sound effects, built in code

function snote(n,i,p,w,v)
 poke2(0x3200+n*68+i*2,p|(w<<6)|(v<<9))
end

function sfx_head(n,spd,lps,lpe)
 local a=0x3200+n*68
 poke(a+64,0)
 poke(a+65,spd)
 poke(a+66,lps)
 poke(a+67,lpe)
end

-- (the loading noises themselves
-- are the real tape audio now)
function make_sfx()
 -- 2: found chirp
 for i=0,31 do
  snote(2,i,i<16 and 30+i*2 or 0,0,i<16 and 4 or 0)
 end
 sfx_head(2,2,0,0)
 -- 3: error buzz
 for i=0,31 do
  snote(3,i,12,2,i<20 and 4 or 0)
 end
 sfx_head(3,3,0,0)
end
