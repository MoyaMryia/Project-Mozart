#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <sys/syscall.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

// 只替换被测库的系统调用，构造持续 EAGAIN、EINTR 和端点故障。
enum fault { NONE, READ_AGAIN, READ_INTR, READ_ERROR, READ_ZERO,
             WRITE_AGAIN, WRITE_INTR, WRITE_ERROR, WRITE_ZERO,
             POLL_INTR, POLL_ERROR, POLL_HANGUP, POLL_EMPTY,
             BLOCK_READ, BLOCK_WRITE, TIMER_ARM_ERROR, TIMER_DISARM_ERROR,
             TIMER_CREATE_ERROR };
static enum fault fault;
static uint64_t fake_us;
static int polls, first_timeout, last_timeout;
static size_t write_limit;
static int timer_arms, timer_disarms, blocking_calls, blocking_interrupts;
static uint64_t armed_deadline;
static bool changed_deadline, invalid_timer_config;
static bool delay_before_io, signal_before_io;

static void expire_before_io(void)
{
    if(!delay_before_io) return;
    struct timespec delay={.tv_sec=2};
    signal_before_io=nanosleep(&delay,NULL)<0 && errno==EINTR;
    delay_before_io=false;
}

static int test_timer_create(clockid_t clock, struct sigevent *event, timer_t *timer)
{
    if(clock!=CLOCK_MONOTONIC || event->sigev_notify!=SIGEV_THREAD_ID ||
       event->sigev_signo!=SIGUSR1 || event->_sigev_un._tid!=(pid_t)syscall(SYS_gettid))
        invalid_timer_config=true;
    if(fault==TIMER_CREATE_ERROR) { errno=EAGAIN; return -1; }
    return timer_create(clock,event,timer);
}
static int test_timer_settime(timer_t timer, int flags, const struct itimerspec *value,
                              struct itimerspec *old)
{
    bool arm=value->it_value.tv_sec || value->it_value.tv_nsec;
    if(arm) {
        uint64_t deadline=(uint64_t)value->it_value.tv_sec*1000000+(uint64_t)value->it_value.tv_nsec/1000;
        if(timer_arms && armed_deadline!=deadline) changed_deadline=true;
        armed_deadline=deadline; timer_arms++;
        if(flags!=TIMER_ABSTIME || !value->it_interval.tv_nsec) invalid_timer_config=true;
    } else timer_disarms++;
    // 假时钟只记录定时器操作，避免把 fake_us 作为真实过期时间投递信号。
    if(fake_us) {
        if((arm && fault==TIMER_ARM_ERROR) || (!arm && fault==TIMER_DISARM_ERROR)) {
            errno=EIO; return -1;
        }
        return 0;
    }
    return timer_settime(timer,flags,value,old);
}

static int test_clock_gettime(clockid_t id, struct timespec *t)
{
    if(!fake_us) return clock_gettime(id,t);
    t->tv_sec=(time_t)(fake_us/1000000); t->tv_nsec=(long)(fake_us%1000000)*1000;
    return 0;
}
static int test_poll(struct pollfd *p, nfds_t n, int timeout)
{
    if(fault==BLOCK_READ || fault==BLOCK_WRITE) { polls++; p->revents=p->events; return 1; }
    if(!fake_us) return poll(p,n,timeout);
    polls++; if(polls==1) first_timeout=timeout; last_timeout=timeout;
    fake_us+=10000;
    if(fault==POLL_INTR) { errno=EINTR; return -1; }
    if(fault==POLL_ERROR) { errno=EIO; return -1; }
    p->revents=fault==POLL_HANGUP?POLLHUP:fault==POLL_EMPTY?0:p->events;
    return 1;
}
static ssize_t test_read(int fd, void *p, size_t n)
{
    if(fault==READ_AGAIN || fault==READ_INTR || fault==READ_ERROR) {
        errno=fault==READ_AGAIN?EAGAIN:fault==READ_INTR?EINTR:EIO; return -1;
    }
    if(fault==READ_ZERO) return 0;
    if(fault==BLOCK_READ) { expire_before_io(); blocking_calls++; }
    ssize_t result=read(fd,p,n);
    if(fault==BLOCK_READ && result<0 && errno==EINTR) blocking_interrupts++;
    return result;
}
static ssize_t test_write(int fd, const void *p, size_t n)
{
    if(fault==WRITE_AGAIN || fault==WRITE_INTR || fault==WRITE_ERROR) {
        errno=fault==WRITE_AGAIN?EAGAIN:fault==WRITE_INTR?EINTR:EIO; return -1;
    }
    if(fault==WRITE_ZERO) return 0;
    if(write_limit && n>write_limit) n=write_limit;
    if(fault==BLOCK_WRITE) { expire_before_io(); blocking_calls++; }
    ssize_t result=write(fd,p,n);
    if(fault==BLOCK_WRITE && result<0 && errno==EINTR) blocking_interrupts++;
    return result;
}
#define timer_create test_timer_create
#define timer_settime test_timer_settime
#define clock_gettime test_clock_gettime
#define poll test_poll
#define read test_read
#define write test_write
#include "../src/usb_link.c"
#undef timer_create
#undef timer_settime
#undef clock_gettime
#undef poll
#undef read
#undef write

static int failures;
#define CHECK(x) do { if(!(x)){fprintf(stderr,"FAIL %s:%d: %s\n",__FILE__,__LINE__,#x);failures++;} } while(0)

// 阻塞回归必须自行失败；SIGALRM 与被测链路专用的 SIGUSR1 分离。
static void test_watchdog(int signo)
{
    (void)signo;
    static const char reason[]="FAIL test_usb_link: blocking I/O watchdog timeout\n";
    ssize_t ignored=write(STDERR_FILENO,reason,sizeof(reason)-1);
    (void)ignored;
    _exit(1);
}

// 独立协议字节夹具，按 Python struct.pack("<HB I H", 1, 1, 8, 64) 排列。
static const unsigned char hello64[]={1,0,1,8,0,0,0,64,0};
static const unsigned char ack64[]={2,0,1,8,0,0,0,64,0};
static const unsigned char ack128[]={2,0,1,8,0,0,0,128,0};
static const unsigned char start64[]={3,0,64,0}, start128[]={3,0,128,0};
static const unsigned char stop[]={4,0};
static unsigned char payload[512];

static size_t make_frame(unsigned char *dst,uint8_t type,uint8_t stream,uint32_t seq,uint64_t ts,const void *body,uint32_t len)
{
    ul_header(dst,type,stream,seq,ts,len);
    if(len) memcpy(dst+MOZART_USB_HEADER_BYTES,body,len);
    return MOZART_USB_HEADER_BYTES+len;
}
static timer_t fixture_timer;
static bool have_fixture_timer;
static void reset(mozart_usblink_t *l)
{
    if(have_fixture_timer) CHECK(timer_delete(fixture_timer)==0);
    memset(l,0,sizeof(*l)); l->fd_out=-1; l->fd_in=-1;
    // 每个夹具均创建真实定时器，与公开 open 使用同一个初始化函数。
    if(ul_timer_init(l)<0) { fprintf(stderr,"fixture: %s\n",l->error); exit(1); }
    fixture_timer=l->io_timer; have_fixture_timer=true;
    timer_arms=timer_disarms=0; armed_deadline=0; changed_deadline=false;
}
static int control(mozart_usblink_t *l,const unsigned char *body,size_t n)
{
    int16_t pcm[256];
    l->pending_len=make_frame(l->pending,1,1,99,123,body,(uint32_t)n);
    return ul_decode(l,pcm,128,ul_deadline(UL_READ_TIMEOUT_MS));
}
static int audio(mozart_usblink_t *l,int block,uint32_t seq)
{
    int16_t pcm[256];
    l->pending_len=make_frame(l->pending,0,block==64?3:2,seq,0x123456789ull,payload,(uint32_t)block*4);
    return ul_decode(l,pcm,128,ul_deadline(UL_READ_TIMEOUT_MS));
}
static bool receive(int fd,unsigned char *p,size_t n)
{
    size_t done=0;
    while(done<n) {
        struct pollfd wait={.fd=fd,.events=POLLIN};
        if(poll(&wait,1,1000)!=1) { CHECK(false); return false; }
        ssize_t r=read(fd,p+done,n-done);
        if(r<=0) { CHECK(false); return false; }
        done+=(size_t)r;
    }
    return true;
}
static void expect_header(const unsigned char *frame,uint8_t type,uint8_t stream,uint32_t seq,size_t bytes)
{
    CHECK(frame[0]==0x5a && frame[1]==0x4d && frame[2]==1);
    CHECK(frame[3]==type && frame[4]==stream);
    CHECK(frame[5]==0 && frame[6]==0 && frame[7]==0);
    CHECK(ul_u32(frame+8)==seq && ul_u32(frame+20)==bytes);
}
static void expect_control(int fd,const unsigned char *body,size_t n,uint32_t seq)
{
    unsigned char frame[64];
    if(!receive(fd,frame,24+n)) return;
    expect_header(frame,1,1,seq,n);
    CHECK(!memcmp(frame+24,body,n));
}
static void expect_audio(mozart_usblink_t *l,int peer,int block,uint32_t seq)
{
    int16_t pcm[256]; unsigned char frame[24+512];
    memcpy(pcm,payload,sizeof(pcm));
    int written=mozart_usblink_write_audio(l,pcm,block);
    CHECK(written==block);
    if(written!=block) { fprintf(stderr,"audio write: %s\n",mozart_usblink_last_error(l)); return; }
    if(!receive(peer,frame,24+(size_t)block*4)) return;
    expect_header(frame,0,block==64?3:2,seq,(size_t)block*4);
    if(block==64) CHECK(ul_u64(frame+12)==0x123456789ull);
    CHECK(!memcmp(frame+24,payload,(size_t)block*4));
}

static void test_golden_and_negotiation(void)
{
    mozart_usblink_t l; int16_t pcm[256]; reset(&l);
    // 固定旧版帧头：序列号 5，时间戳 0x1122334455667788。
    static const unsigned char golden_header[24]={0x5a,0x4d,1,0,2,0,0,0,5,0,0,0,0x88,0x77,0x66,0x55,0x44,0x33,0x22,0x11,0,2,0,0};
    memcpy(l.pending,golden_header,24); memcpy(l.pending+24,payload,512); l.pending_len=536;
    CHECK(ul_decode(&l,pcm,128,ul_deadline(UL_READ_TIMEOUT_MS))==128 && l.rx_frames==1 && !l.pending_len);
    CHECK(!memcmp(pcm,payload,512));
    CHECK(l.metadata[0].seq==5 && l.metadata[0].ts==0x1122334455667788ull);
    CHECK(mozart_usblink_block_frames(&l)==128 && !mozart_usblink_negotiated(&l));
    int s[2]; CHECK(socketpair(AF_UNIX,SOCK_STREAM,0,s)==0); l.fd_in=s[0];
    CHECK(control(&l,hello64,sizeof(hello64))==0);
    CHECK(l.candidate64 && !l.negotiated && mozart_usblink_block_frames(&l)==128);
    expect_control(s[1],ack64,sizeof(ack64),0);
    expect_audio(&l,s[1],128,5);
    // HELLO 后未 START，旧版音频继续正常；控制帧不占用音频序列号。
    CHECK(audio(&l,128,6)==128); expect_audio(&l,s[1],128,6);
    CHECK(control(&l,hello64,sizeof(hello64))==0); expect_control(s[1],ack64,sizeof(ack64),1);
    CHECK(audio(&l,128,7)==128); expect_audio(&l,s[1],128,7);
    CHECK(control(&l,start64,sizeof(start64))==0);
    static const unsigned char start_ack[]={5,0,64,0};
    expect_control(s[1],start_ack,sizeof(start_ack),2);
    CHECK(l.negotiated && l.generation==1 && !l.have_seq && !l.meta_count);
    CHECK(audio(&l,64,37)==64); expect_audio(&l,s[1],64,37);
    CHECK(mozart_usblink_tx_frames(&l)==1 && mozart_usblink_rx_frames(&l)==1);
    CHECK(mozart_usblink_pending_frames(&l)==0 && mozart_usblink_block_frames(&l)==64);
    // 新 HELLO 不改变已经启动的 64 采样帧会话。
    unsigned char hello128[9]; memcpy(hello128,hello64,9); hello128[7]=128;
    CHECK(control(&l,hello128,9)==0); expect_control(s[1],ack128,9,3);
    CHECK(l.negotiated && !l.candidate64); CHECK(audio(&l,64,38)==64); expect_audio(&l,s[1],64,38);
    CHECK(control(&l,start128,4)==0);
    static const unsigned char start_ack128[]={5,0,128,0}; expect_control(s[1],start_ack128,4,4);
    CHECK(!l.negotiated && l.generation==2); CHECK(audio(&l,128,0)==128); expect_audio(&l,s[1],128,0);
    close(s[0]); close(s[1]);

    // 缺少能力、请求 128、旧版误填采样率或版本不符时，ACK 选择 128。
    for(int kind=0;kind<4;kind++) {
        reset(&l); CHECK(socketpair(AF_UNIX,SOCK_STREAM,0,s)==0); l.fd_in=s[0];
        unsigned char hello[9]; memcpy(hello,hello64,9);
        if(kind==0) hello[3]=0;
        if(kind==1) hello[7]=128;
        if(kind==2) { hello[7]=0x80; hello[8]=0xbb; }
        if(kind==3) hello[2]=2;
        CHECK(control(&l,hello,9)==0 && !l.candidate64 && !l.negotiated);
        expect_control(s[1],ack128,9,0);
        CHECK(control(&l,start64,4)<0 && strstr(l.error,"unsupported START"));
        close(s[0]); close(s[1]);
    }
}

static void test_parser_and_guards(void)
{
    mozart_usblink_t l; int16_t pcm[256]; unsigned char frame[24+512]; int p[2];
    // 未知类型和未知流之后，同一读取缓冲区内的已知帧必须立即返回。
    for(int kind=0;kind<2;kind++) {
        reset(&l); CHECK(pipe(p)==0); l.fd_out=p[0];
        unsigned char batch[24+3+24+512], junk[3]={1,2,3};
        size_t n=make_frame(batch,kind?0:99,99,0,0,junk,3);
        n+=make_frame(batch+n,0,2,8,99,payload,512);
        CHECK(write(p[1],batch,n)==(ssize_t)n);
        CHECK(mozart_usblink_read_audio(&l,pcm,128)==128 && !l.pending_len);
        CHECK(l.metadata[0].seq==8 && l.metadata[0].ts==99);
        close(p[0]); close(p[1]);
    }
    // 分片读取保留帧前缀；第二次调用完成解码。
    reset(&l); CHECK(pipe(p)==0); l.fd_out=p[0];
    size_t n=make_frame(frame,0,2,0,55,payload,512);
    CHECK(write(p[1],frame,11)==11);
    CHECK(mozart_usblink_read_audio(&l,pcm,128)==0 && l.pending_len==11);
    CHECK(write(p[1],frame+11,n-11)==(ssize_t)(n-11));
    CHECK(mozart_usblink_read_audio(&l,pcm,128)==128); close(p[0]); close(p[1]);
    // 每个畸形输入都返回失败，并记录对应错误。
    static const char *errors[]={"magic","version","reserved","payload exceeds","payload length","negotiated block","sequence","capacity"};
    for(int kind=0;kind<8;kind++) {
        reset(&l); n=make_frame(l.pending,0,2,6,0,payload,512); l.pending_len=n;
        if(kind==0) l.pending[0]^=1;
        if(kind==1) l.pending[2]=2;
        if(kind==2) l.pending[6]=1;
        if(kind==3) ul_put32(l.pending+20,MOZART_USB_MAX_PAYLOAD+1);
        if(kind==4) ul_put32(l.pending+20,511);
        if(kind==5) { l.pending[4]=3; ul_put32(l.pending+20,256); }
        if(kind==6) { l.have_seq=true; l.expected_seq=7; }
        CHECK(mozart_usblink_read_audio(&l,pcm,kind==7?127:128)<0);
        CHECK(strstr(mozart_usblink_last_error(&l),errors[kind])!=NULL);
    }
    reset(&l); l.negotiated=true; CHECK(audio(&l,128,0)<0);
    CHECK(audio(&l,64,9)==64); CHECK(audio(&l,64,0)<0 && strstr(l.error,"sequence"));
    reset(&l); CHECK(audio(&l,128,9)==128); CHECK(audio(&l,128,0)==128);
    CHECK(l.generation==1 && l.rx_frames==1 && l.meta_count==1);
    reset(&l);
    for(uint32_t seq=0;seq<UL_META_CAP;seq++) CHECK(audio(&l,128,seq)==128);
    CHECK(audio(&l,128,UL_META_CAP)<0 && strstr(l.error,"overflow"));
    // 控制体长度校验独立覆盖 HELLO、START、STOP 和操作码不足。
    static const unsigned char bad[][4]={{0},{1,0},{3,0,64},{4,0,0}};
    static const size_t lengths[]={1,2,3,3};
    for(size_t i=0;i<4;i++) { reset(&l); CHECK(control(&l,bad[i],lengths[i])<0); CHECK(l.error[0]); }
    reset(&l); CHECK(control(&l,start64,4)<0);
    CHECK(strstr(l.error,"unsupported START"));
    static const unsigned char start32[]={3,0,32,0};
    reset(&l); l.candidate64=true; CHECK(control(&l,start32,4)<0);
    CHECK(strstr(l.error,"unsupported START"));
    reset(&l); CHECK(mozart_usblink_write_audio(&l,pcm,64)<0 && strstr(l.error,"block size"));
    CHECK(mozart_usblink_write_audio(&l,pcm,128)<0 && strstr(l.error,"empty"));
    CHECK(mozart_usblink_read_audio(NULL,pcm,128)<0 && mozart_usblink_read_audio(&l,NULL,128)<0);
    CHECK(mozart_usblink_write_audio(NULL,pcm,128)<0 && mozart_usblink_write_audio(&l,NULL,128)<0);
    CHECK(mozart_usblink_finish(NULL)<0);
}

static void test_stop_restart(void)
{
    mozart_usblink_t l; int s[2]; reset(&l);
    CHECK(socketpair(AF_UNIX,SOCK_STREAM,0,s)==0); l.fd_in=s[0];
    CHECK(control(&l,hello64,9)==0); expect_control(s[1],ack64,9,0);
    CHECK(control(&l,start64,4)==0);
    static const unsigned char start_ack[]={5,0,64,0}; expect_control(s[1],start_ack,4,1);
    for(int session=0;session<2;session++) {
        CHECK(audio(&l,64,0)==64);
        CHECK(control(&l,stop,2)==0 && mozart_usblink_stop_requested(&l));
        CHECK(mozart_usblink_finish(&l)<0 && strstr(l.error,"not drained"));
        expect_audio(&l,s[1],64,0);
        CHECK(mozart_usblink_finish(&l)==0 && l.stop_ack_sent);
        static const unsigned char stop_ack[]={6,0,1,0,0,0,1,0,0,0};
        expect_control(s[1],stop_ack,10,session?4:2);
        CHECK(mozart_usblink_finish(&l)==0);
        struct pollfd wait={.fd=s[1],.events=POLLIN}; CHECK(poll(&wait,1,0)==0);
        if(session==0) {
            CHECK(control(&l,start64,4)==0); expect_control(s[1],start_ack,4,3);
            CHECK(!l.stopped && !l.stop_ack_sent && !l.have_seq && !l.expected_seq);
            CHECK(!l.rx_stop_count && !l.rx_frames && !l.tx_frames && !l.meta_count && !l.meta_head);
            CHECK(!l.error[0] && mozart_usblink_generation(&l)==2);
        }
    }
    // STOP_ACK 后从端点接收新 START，验证公开读取接口可以启动下一会话。
    int p[2]; CHECK(pipe(p)==0); l.fd_out=p[0];
    unsigned char batch[24+4+24+256]; int16_t pcm[128];
    size_t n=make_frame(batch,1,1,0,0,start64,4);
    n+=make_frame(batch+n,0,3,0,0x123456789ull,payload,256);
    CHECK(write(p[1],batch,n)==(ssize_t)n);
    CHECK(mozart_usblink_read_audio(&l,pcm,64)==64);
    expect_control(s[1],start_ack,4,5);
    CHECK(!mozart_usblink_stop_requested(&l) && !l.stop_ack_sent && l.generation==3);
    expect_audio(&l,s[1],64,0);
    CHECK(control(&l,stop,2)==0 && mozart_usblink_finish(&l)==0);
    static const unsigned char final_stop_ack[]={6,0,1,0,0,0,1,0,0,0};
    expect_control(s[1],final_stop_ack,10,6);
    n=make_frame(batch,0,3,1,0,payload,256);
    CHECK(write(p[1],batch,n)==(ssize_t)n);
    CHECK(mozart_usblink_read_audio(&l,pcm,64)<0 && strstr(l.error,"after STOP"));
    CHECK(l.rx_frames==1 && l.tx_frames==1 && !l.meta_count);
    close(p[0]); close(p[1]); close(s[0]); close(s[1]);
}

static void test_explicit128_sessions(void)
{
    mozart_usblink_t l; int s[2], p[2]; reset(&l);
    CHECK(socketpair(AF_UNIX,SOCK_STREAM,0,s)==0 && pipe(p)==0);
    l.fd_in=s[0]; l.fd_out=p[0];
    const int blocks[]={128,128,64,128}, counts[]={1,3,2,1};
    uint32_t control_seq=0;
    for(size_t session=0;session<4;session++) {
        // 首次 START128 无 HELLO；后续 START128 覆盖仍为真的 candidate64。
        if(session==1) {
            CHECK(control(&l,hello64,9)==0); expect_control(s[1],ack64,9,control_seq++);
        }
        int block=blocks[session];
        unsigned char batch[24+4+24+512], ack[]={5,0,0,0}; int16_t pcm[256];
        size_t n=make_frame(batch,1,1,0,0,block==64?start64:start128,4);
        n+=make_frame(batch+n,0,block==64?3:2,0,0x123456789ull,payload,(uint32_t)block*4);
        CHECK(write(p[1],batch,n)==(ssize_t)n);
        CHECK(mozart_usblink_read_audio(&l,pcm,128)==block);
        ul_put16(ack+2,(uint16_t)block); expect_control(s[1],ack,4,control_seq++);
        CHECK(l.generation==session+1 && !l.stopped && !l.stop_ack_sent && !l.error[0]);
        CHECK(l.negotiated==(block==64) && mozart_usblink_block_frames(&l)==block);
        CHECK(l.rx_frames==1 && !l.tx_frames && l.meta_head==0 && l.meta_count==1 && !l.rx_stop_count);
        expect_audio(&l,s[1],block,0);
        for(int seq=1;seq<counts[session];seq++) {
            CHECK(audio(&l,block,(uint32_t)seq)==block); expect_audio(&l,s[1],block,(uint32_t)seq);
        }
        CHECK(control(&l,stop,2)==0 && mozart_usblink_stop_requested(&l));
        CHECK(mozart_usblink_finish(&l)==0);
        unsigned char stopped[]={6,0,0,0,0,0,0,0,0,0};
        ul_put32(stopped+2,(uint32_t)counts[session]); ul_put32(stopped+6,(uint32_t)counts[session]);
        expect_control(s[1],stopped,10,control_seq++);
        CHECK(l.rx_frames==(uint64_t)counts[session] && l.tx_frames==l.rx_frames && !l.meta_count);
    }
    // STOP 后 seq0 和连续序列号都失败，旧 raw 客户端须重启 pre。
    CHECK(audio(&l,128,0)<0 && strstr(l.error,"after STOP"));
    CHECK(audio(&l,128,1)<0 && strstr(l.error,"after STOP"));
    CHECK(l.rx_frames==1 && l.tx_frames==1 && !l.meta_count);
    close(p[0]); close(p[1]); close(s[0]); close(s[1]);
}

static void test_io_deadlines(void)
{
    mozart_usblink_t l; int16_t pcm[256]; unsigned char bytes[4]={0};
    // 假时钟每次 poll 前进 10 ms，持续可重试错误必须消耗同一个预算。
    static const enum fault retries[]={READ_AGAIN,READ_INTR,WRITE_AGAIN,WRITE_INTR,POLL_INTR,POLL_EMPTY};
    for(size_t i=0;i<sizeof(retries)/sizeof(retries[0]);i++) {
        for(int writing=0;writing<2;writing++) {
            if((retries[i]==READ_AGAIN || retries[i]==READ_INTR) && writing) continue;
            if((retries[i]==WRITE_AGAIN || retries[i]==WRITE_INTR) && !writing) continue;
            reset(&l); fault=retries[i]; fake_us=10000000; polls=0;
            int r=writing?ul_write_all(&l,bytes,4,ul_deadline(MOZART_USB_IO_TIMEOUT_MS)):mozart_usblink_read_audio(&l,pcm,128);
            int budget=writing?MOZART_USB_IO_TIMEOUT_MS:UL_READ_TIMEOUT_MS;
            CHECK(writing?r<0:r==0);
            if(writing) CHECK(strstr(l.error,"timeout"));
            CHECK(polls==budget/10 && fake_us==10000000+(uint64_t)budget*1000);
            CHECK(first_timeout==budget && last_timeout==10);
            CHECK(!changed_deadline && timer_arms==timer_disarms);
            if(timer_arms) CHECK(armed_deadline==10000000+(uint64_t)budget*1000);
            fault=NONE; fake_us=0;
        }
    }
    static const enum fault errors[]={READ_ERROR,READ_ZERO,WRITE_ERROR,WRITE_ZERO,POLL_ERROR,POLL_HANGUP};
    for(size_t i=0;i<sizeof(errors)/sizeof(errors[0]);i++) {
        reset(&l); fault=errors[i]; fake_us=10000000; polls=0;
        bool writing=fault==WRITE_ERROR || fault==WRITE_ZERO;
        int r=writing?ul_write_all(&l,bytes,4,ul_deadline(MOZART_USB_IO_TIMEOUT_MS)):mozart_usblink_read_audio(&l,pcm,128);
        CHECK(fault==READ_ZERO?r==0:r<0);
        if(fault!=READ_ZERO) CHECK(l.error[0]);
        fault=NONE; fake_us=0;
    }
    // 连续 HELLO 回复共用读取预算；到期后保留后续控制帧和音频。
    int s[2]; CHECK(socketpair(AF_UNIX,SOCK_STREAM,0,s)==0); reset(&l); l.fd_in=s[0];
    for(int i=0;i<20;i++) l.pending_len+=make_frame(l.pending+l.pending_len,1,1,(uint32_t)i,0,hello64,9);
    l.pending_len+=make_frame(l.pending+l.pending_len,0,2,0,0,payload,512);
    fake_us=10000000; polls=0;
    CHECK(mozart_usblink_read_audio(&l,pcm,128)==0);
    CHECK(fake_us==10000000+(uint64_t)UL_READ_TIMEOUT_MS*1000 && l.pending_len>0);
    CHECK(mozart_usblink_read_audio(&l,pcm,128)==0);
    CHECK(fake_us==10000000+(uint64_t)UL_READ_TIMEOUT_MS*2000 && l.pending_len==24+512);
    CHECK(mozart_usblink_read_audio(&l,pcm,128)==128 && !l.pending_len);
    fake_us=0;
    for(uint32_t i=0;i<20;i++) expect_control(s[1],ack64,9,i);
    close(s[0]); close(s[1]);
    // 控制回复遇到持续 EAGAIN 时，也按外层读取截止时间失败。
    CHECK(socketpair(AF_UNIX,SOCK_STREAM,0,s)==0); reset(&l); l.fd_in=s[0];
    l.pending_len=make_frame(l.pending,1,1,0,0,hello64,9);
    fault=WRITE_AGAIN; fake_us=10000000; polls=0;
    CHECK(mozart_usblink_read_audio(&l,pcm,128)<0 && strstr(l.error,"timeout"));
    CHECK(fake_us==10000000+(uint64_t)UL_READ_TIMEOUT_MS*1000);
    fault=NONE; fake_us=0; close(s[0]); close(s[1]);
    // 真实非阻塞套接字写满后，写操作也必须在总截止时间内失败。
    CHECK(socketpair(AF_UNIX,SOCK_STREAM,0,s)==0); reset(&l); l.fd_in=s[0];
    CHECK(fcntl(s[0],F_SETFL,O_NONBLOCK)==0);
    unsigned char fill[4096]={0};
    while(write(s[0],fill,sizeof(fill))>0) {}
    CHECK(errno==EAGAIN || errno==EWOULDBLOCK);
    uint64_t begin=ul_now_us(); CHECK(ul_write_all(&l,bytes,4,ul_deadline(MOZART_USB_IO_TIMEOUT_MS))<0); uint64_t elapsed=ul_now_us()-begin;
    uint64_t budget_us=(uint64_t)MOZART_USB_IO_TIMEOUT_MS*1000;
    CHECK(strstr(l.error,"timeout") && elapsed>=budget_us && elapsed<budget_us+500000);
    close(s[0]); close(s[1]);
    // 部分写入按偏移继续发送，不丢失头部或负载。
    CHECK(socketpair(AF_UNIX,SOCK_STREAM,0,s)==0); reset(&l); l.fd_in=s[0];
    CHECK(audio(&l,128,19)==128); write_limit=31; expect_audio(&l,s[1],128,19); write_limit=0;
    close(s[0]); close(s[1]);
}

static void test_blocking_syscalls(void)
{
    // poll 恒报 ready；空套接字的实际阻塞 read 必须由真实 SIGUSR1 中断。
    for(int writing=0;writing<2;writing++) {
        for(int early_signal=0;early_signal<2;early_signal++) {
            mozart_usblink_t l; int s[2]; int16_t pcm[256]={0}; reset(&l);
            CHECK(socketpair(AF_UNIX,SOCK_STREAM,0,s)==0);
            l.fd_out=l.fd_in=s[0];
            if(writing) {
                CHECK(audio(&l,128,11)==128);
                CHECK(fcntl(s[0],F_SETFL,O_NONBLOCK)==0);
                unsigned char fill[4096]={0};
                while(write(s[0],fill,sizeof(fill))>0) {}
                CHECK(errno==EAGAIN || errno==EWOULDBLOCK);
            }
            CHECK(fcntl(s[0],F_SETFL,0)==0);
            CHECK(!(fcntl(s[0],F_GETFL)&O_NONBLOCK));
            fault=writing?BLOCK_WRITE:BLOCK_READ;
            polls=blocking_calls=blocking_interrupts=0;
            delay_before_io=early_signal; signal_before_io=false;
            uint64_t begin=ul_now_us();
            int result=writing?mozart_usblink_write_audio(&l,pcm,128):mozart_usblink_read_audio(&l,pcm,128);
            uint64_t elapsed=ul_now_us()-begin;
            uint64_t budget=(uint64_t)(writing?MOZART_USB_IO_TIMEOUT_MS:UL_READ_TIMEOUT_MS)*1000;
            CHECK(writing?result<0:result==0);
            CHECK(elapsed>=budget && elapsed<budget+500000);
            CHECK(polls>0 && blocking_calls>0 && blocking_interrupts>0);
            CHECK(timer_arms==timer_disarms && !changed_deadline);
            if(early_signal) CHECK(signal_before_io);
            if(writing) CHECK(strstr(l.error,"timeout") && l.meta_count==1 && !l.tx_frames);
            struct itimerspec timer;
            CHECK(timer_gettime(l.io_timer,&timer)==0);
            CHECK(!timer.it_value.tv_sec && !timer.it_value.tv_nsec);
            printf("[OK] blocking %s%s: %.3f ms\n",writing?"write":"read",
                   early_signal?" (signal before syscall)":"",(double)elapsed/1000);
            fault=NONE; close(s[0]); close(s[1]);
        }
    }
}

static void test_timer_failures(void)
{
    for(int writing=0;writing<2;writing++) {
        for(int disarming=0;disarming<2;disarming++) {
            mozart_usblink_t l; int s[2]; int16_t pcm[256]; unsigned char byte=0;
            reset(&l); CHECK(socketpair(AF_UNIX,SOCK_STREAM,0,s)==0);
            l.fd_out=l.fd_in=s[0];
            CHECK(write(s[1],&byte,1)==1);
            fault=disarming?TIMER_DISARM_ERROR:TIMER_ARM_ERROR;
            fake_us=10000000; polls=0;
            int result=writing?ul_write_all(&l,&byte,1,ul_deadline(MOZART_USB_IO_TIMEOUT_MS)):
                               mozart_usblink_read_audio(&l,pcm,128);
            CHECK(result<0 && errno==EIO);
            CHECK(strstr(l.error,disarming?"timer disarm failed":"timer arm failed"));
            CHECK(timer_arms==1 && timer_disarms==disarming);
            CHECK(!l.pending_len);
            fault=NONE; fake_us=0; close(s[0]); close(s[1]);
        }
    }
}

static void test_open(void)
{
    CHECK(mozart_usblink_open(NULL)==NULL);
    char long_path[600]; memset(long_path,'x',sizeof(long_path)-1); long_path[599]='\0';
    errno=0; CHECK(mozart_usblink_open(long_path)==NULL && errno==ENAMETOOLONG);
    char dir[]="/tmp/mozart-usblink-XXXXXX"; CHECK(mkdtemp(dir)!=NULL);
    CHECK(mozart_usblink_open(dir)==NULL);
    char path[128]; snprintf(path,sizeof(path),"%s/ep3",dir); CHECK(mkfifo(path,0600)==0);
    CHECK(mozart_usblink_open(dir)==NULL);
    snprintf(path,sizeof(path),"%s/ep4",dir); CHECK(mkfifo(path,0600)==0);
    int reader=open(path,O_RDONLY|O_NONBLOCK); CHECK(reader>=0);
    fault=TIMER_CREATE_ERROR; errno=0;
    CHECK(mozart_usblink_open(dir)==NULL && errno==EAGAIN);
    fault=NONE;
    mozart_usblink_t *l=mozart_usblink_open(dir); CHECK(l!=NULL);
    if(l) {
        int out_flags=fcntl(l->fd_out,F_GETFL), in_flags=fcntl(l->fd_in,F_GETFL);
        CHECK(out_flags>=0 && (out_flags&O_NONBLOCK));
        CHECK(in_flags>=0 && (in_flags&O_NONBLOCK));
        struct sigaction action; sigset_t mask;
        CHECK(sigaction(SIGUSR1,NULL,&action)==0);
        CHECK(action.sa_handler==ul_io_signal && !(action.sa_flags&SA_RESTART));
        CHECK(sigprocmask(SIG_SETMASK,NULL,&mask)==0 && !sigismember(&mask,SIGUSR1));
        timer_t timer=l->io_timer;
        mozart_usblink_close(l);
        struct itimerspec value;
        CHECK(timer_gettime(timer,&value)<0 && errno==EINVAL);
    }
    close(reader); CHECK(unlink(path)==0);
    snprintf(path,sizeof(path),"%s/ep3",dir); CHECK(unlink(path)==0); CHECK(rmdir(dir)==0);
    mozart_usblink_close(NULL);
}

int main(void)
{
    struct sigaction watchdog={0};
    watchdog.sa_handler=test_watchdog;
    sigemptyset(&watchdog.sa_mask);
    if(sigaction(SIGALRM,&watchdog,NULL)<0) { perror("test watchdog sigaction"); return 1; }
    alarm(15);
    for(size_t i=0;i<sizeof(payload);i++) payload[i]=(unsigned char)((i*7+3)%256);
    test_golden_and_negotiation(); test_parser_and_guards(); test_stop_restart(); test_explicit128_sessions(); test_io_deadlines();
    test_blocking_syscalls(); test_timer_failures(); test_open();
    if(have_fixture_timer) CHECK(timer_delete(fixture_timer)==0);
    CHECK(!invalid_timer_config);
    alarm(0);
    if(failures) { fprintf(stderr,"test_usb_link: %d failures\n",failures); return 1; }
    puts("[OK] test_usb_link"); return 0;
}
