#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#define _POSIX_C_SOURCE 200809L
#include "mozart/usb_link.h"
#include "mozart/usb_protocol_generated.h"

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/syscall.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define UL_PENDING_CAP (MOZART_USB_HEADER_BYTES + MOZART_USB_MAX_PAYLOAD)
#define UL_META_CAP 32
#define UL_READ_CAP 4096
#define UL_READ_TIMEOUT_MS 100

typedef struct { uint32_t seq; uint64_t ts; } ul_meta_t;
struct mozart_usblink {
    int fd_out, fd_in;
    timer_t io_timer;
    uint32_t control_seq, expected_seq;
    uint64_t rx_frames, tx_frames, generation;
    bool have_seq, candidate64, negotiated, stopped, stop_ack_sent;
    uint32_t rx_stop_count;
    unsigned char pending[UL_PENDING_CAP];
    size_t pending_len;
    ul_meta_t metadata[UL_META_CAP];
    size_t meta_head, meta_count;
    char error[192];
};

static uint16_t ul_u16(const unsigned char *p) { return (uint16_t)(p[0] | (uint16_t)p[1] << 8); }
static uint32_t ul_u32(const unsigned char *p) { return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24; }
static uint64_t ul_u64(const unsigned char *p) { return (uint64_t)ul_u32(p) | (uint64_t)ul_u32(p + 4) << 32; }
static void ul_put16(unsigned char *p, uint16_t v) { p[0]=(unsigned char)v; p[1]=(unsigned char)(v>>8); }
static void ul_put32(unsigned char *p, uint32_t v) { for (unsigned i=0;i<4;i++) p[i]=(unsigned char)(v>>(8*i)); }
static void ul_put64(unsigned char *p, uint64_t v) { ul_put32(p,(uint32_t)v); ul_put32(p+4,(uint32_t)(v>>32)); }
static int ul_fail(mozart_usblink_t *l, const char *msg) { snprintf(l->error,sizeof(l->error),"%s",msg); return -1; }

static uint64_t ul_now_us(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return (uint64_t)t.tv_sec*1000000u+(uint64_t)t.tv_nsec/1000u; }

/* Linux API 约定：进程将 SIGUSR1 专用于 preprocessor 的 USB I/O。
 * 在创建链路的主线程调用读写接口，并保持 SIGUSR1 可投递。
 * 空 handler 仅打断系统调用；内部错误由调用方返回并保存 reason。
 * SA_RESTART 保持关闭。定时器直接向创建线程投递信号，不创建辅助线程。
 */
static void ul_io_signal(int signo) { (void)signo; }

static int ul_timer_error(mozart_usblink_t *l, const char *operation)
{
    int saved=errno;
    snprintf(l->error,sizeof(l->error),"USB I/O timer %s failed: %s",operation,strerror(saved));
    errno=saved;
    return -1;
}

static int ul_timer_init(mozart_usblink_t *l)
{
    struct sigaction action={0};
    action.sa_handler=ul_io_signal;
    sigemptyset(&action.sa_mask);
    if(sigaction(SIGUSR1,&action,NULL)<0) return ul_timer_error(l,"sigaction");
    sigset_t signals;
    sigemptyset(&signals); sigaddset(&signals,SIGUSR1);
    if(sigprocmask(SIG_UNBLOCK,&signals,NULL)<0) return ul_timer_error(l,"unblock");
    struct sigevent event={0};
    event.sigev_notify=SIGEV_THREAD_ID;
    event.sigev_signo=SIGUSR1;
    event._sigev_un._tid=(pid_t)syscall(SYS_gettid);
    if(timer_create(CLOCK_MONOTONIC,&event,&l->io_timer)<0) return ul_timer_error(l,"create");
    return 0;
}

static int ul_timer_arm(mozart_usblink_t *l, uint64_t deadline)
{
    struct itimerspec timer={0};
    timer.it_value.tv_sec=(time_t)(deadline/1000000);
    timer.it_value.tv_nsec=(long)(deadline%1000000)*1000;
    // 首次到期使用原截止时间；每隔 1 ms 重发，覆盖信号先于 syscall 到达的窗口。
    timer.it_interval.tv_nsec=1000000;
    if(timer_settime(l->io_timer,TIMER_ABSTIME,&timer,NULL)<0) return ul_timer_error(l,"arm");
    return 0;
}

static int ul_timer_disarm(mozart_usblink_t *l)
{
    const struct itimerspec timer={0};
    if(timer_settime(l->io_timer,0,&timer,NULL)<0) return ul_timer_error(l,"disarm");
    return 0;
}

// 每次调用共用一个绝对截止时间；信号和 EAGAIN 重试不延长等待。
static int ul_wait(int fd, short events, uint64_t deadline)
{
    struct pollfd pfd = { .fd=fd, .events=events };
    for (;;) {
        uint64_t now=ul_now_us();
        if(now>=deadline) return 0;
        int timeout=(int)((deadline-now+999)/1000);
        int r=poll(&pfd,1,timeout);
        if(r<0 && errno==EINTR) continue;
        if(r<=0) return r;
        if(pfd.revents&(POLLERR|POLLHUP|POLLNVAL)) { errno=EIO; return -1; }
        if(pfd.revents&events) return 1;
    }
}
static uint64_t ul_deadline(int timeout_ms) { return ul_now_us()+(uint64_t)timeout_ms*1000; }
static int ul_write_all(mozart_usblink_t *l, const unsigned char *p, size_t n, uint64_t deadline)
{
    size_t done=0;
    while(done<n) {
        int r=ul_wait(l->fd_in,POLLOUT,deadline);
        if(r<=0) return ul_fail(l,r==0?"USB write timeout":"USB write poll failed");
        if(ul_timer_arm(l,deadline)<0) return -1;
        ssize_t w=write(l->fd_in,p+done,n-done);
        int saved=errno;
        if(ul_timer_disarm(l)<0) return -1;
        errno=saved;
        if(w<0) {
            if(errno==EINTR||errno==EAGAIN||errno==EWOULDBLOCK) continue;
            return ul_fail(l,"USB endpoint write failed");
        }
        if(w==0) return ul_fail(l,"USB endpoint write returned zero");
        done+=(size_t)w;
    }
    return 0;
}
static void ul_header(unsigned char *h,uint8_t type,uint8_t stream,uint32_t seq,uint64_t ts,uint32_t len)
{
    memset(h,0,MOZART_USB_HEADER_BYTES); ul_put16(h+MOZART_USB_MAGIC_OFFSET,MOZART_USB_MAGIC); h[MOZART_USB_VERSION_OFFSET]=MOZART_USB_VERSION; h[MOZART_USB_TYPE_OFFSET]=type; h[MOZART_USB_STREAM_OFFSET]=stream; ul_put32(h+MOZART_USB_SEQ_OFFSET,seq); ul_put64(h+MOZART_USB_TIMESTAMP_OFFSET,ts); ul_put32(h+MOZART_USB_LENGTH_OFFSET,len);
}
static int ul_send_control(mozart_usblink_t *l,const unsigned char *body,size_t bytes,uint64_t deadline)
{
    unsigned char frame[MOZART_USB_HEADER_BYTES+MOZART_USB_STOP_ACK_BYTES];
    ul_header(frame,MOZART_USB_TYPE_CONTROL,MOZART_USB_STREAM_CONTROL,l->control_seq++,ul_now_us(),(uint32_t)bytes); memcpy(frame+MOZART_USB_HEADER_BYTES,body,bytes); return ul_write_all(l,frame,MOZART_USB_HEADER_BYTES+bytes,deadline);
}
static int ul_control(mozart_usblink_t *l,const unsigned char *b,uint32_t n,uint64_t deadline)
{
    if(n<2) return ul_fail(l,"short USB control body");
    uint16_t op=ul_u16(b);
    if(op==MOZART_USB_CONTROL_HELLO) {
        if(n!=MOZART_USB_HELLO_BYTES) return ul_fail(l,"invalid HELLO length");
        l->candidate64=(b[2]==MOZART_USB_VERSION && (ul_u32(b+3)&MOZART_USB_CAP_FAST64)!=0 && ul_u16(b+7)==MOZART_USB_FAST64_SAMPLES);
        // HELLO 只更新候选块长，START 才切换当前音频流。
        unsigned char ack[MOZART_USB_HELLO_BYTES]; ul_put16(ack,MOZART_USB_CONTROL_HELLO_ACK); ack[2]=MOZART_USB_VERSION; ul_put32(ack+3,MOZART_USB_CAP_FAST64); ul_put16(ack+7,l->candidate64?MOZART_USB_FAST64_SAMPLES:MOZART_USB_FAST_SAMPLES);
        return ul_send_control(l,ack,sizeof(ack),deadline);
    }
    if(op==MOZART_USB_CONTROL_START) {
        if(n!=MOZART_USB_START_BYTES) return ul_fail(l,"invalid START length");
        const uint16_t block=ul_u16(b+2);
        if(block!=MOZART_USB_FAST_SAMPLES && !(block==MOZART_USB_FAST64_SAMPLES && l->candidate64)) return ul_fail(l,"unsupported START block");
        l->negotiated=block==MOZART_USB_FAST64_SAMPLES; l->generation++; l->have_seq=false; l->expected_seq=0; l->rx_frames=0; l->tx_frames=0; l->meta_head=0; l->meta_count=0; l->rx_stop_count=0;
        l->stopped=false; l->stop_ack_sent=false; l->error[0]='\0';
        unsigned char ack[MOZART_USB_START_BYTES]; ul_put16(ack,MOZART_USB_CONTROL_START_ACK); ul_put16(ack+2,block); return ul_send_control(l,ack,sizeof(ack),deadline);
    }
    if(op==MOZART_USB_CONTROL_STOP) {
        if(n!=2) return ul_fail(l,"invalid STOP length");
        l->stopped=true; l->rx_stop_count=(uint32_t)l->rx_frames; return 0;
    }
    return 0;
}

// 音频帧返回采样帧数；跳过帧、控制帧或数据不足返回 0；错误返回 -1。
static int ul_decode(mozart_usblink_t *l,int16_t *out,int capacity,uint64_t deadline)
{
    if(l->pending_len<MOZART_USB_HEADER_BYTES) return 0;
    const unsigned char *h=l->pending;
    uint16_t magic=ul_u16(h+MOZART_USB_MAGIC_OFFSET), reserved=ul_u16(h+MOZART_USB_RESERVED_OFFSET);
    if(magic!=MOZART_USB_MAGIC) return ul_fail(l,"invalid USB frame magic");
    if(h[MOZART_USB_VERSION_OFFSET]!=MOZART_USB_VERSION) return ul_fail(l,"unsupported USB frame version");
    if(reserved!=0) return ul_fail(l,"nonzero USB frame reserved field");
    uint32_t len=ul_u32(h+MOZART_USB_LENGTH_OFFSET);
    if(len>MOZART_USB_MAX_PAYLOAD) return ul_fail(l,"USB frame payload exceeds limit");
    if(l->pending_len<MOZART_USB_HEADER_BYTES+(size_t)len) return 0;
    const uint8_t type=h[MOZART_USB_TYPE_OFFSET], stream=h[MOZART_USB_STREAM_OFFSET];
    const uint32_t seq=ul_u32(h+MOZART_USB_SEQ_OFFSET);
    const uint64_t ts=ul_u64(h+MOZART_USB_TIMESTAMP_OFFSET);
    int result=0;
    if(type==MOZART_USB_TYPE_CONTROL && stream==MOZART_USB_STREAM_CONTROL) result=ul_control(l,h+MOZART_USB_HEADER_BYTES,len,deadline);
    else if(type==MOZART_USB_TYPE_PCM && (stream==MOZART_USB_STREAM_FAST || stream==MOZART_USB_STREAM_FAST64)) {
        if(l->stopped) return ul_fail(l,"USB audio received after STOP");
        int block=stream==MOZART_USB_STREAM_FAST?MOZART_USB_FAST_SAMPLES:MOZART_USB_FAST64_SAMPLES;
        if(len!=(uint32_t)(block*MOZART_USB_CHANNELS*MOZART_USB_SAMPLE_BYTES)) return ul_fail(l,"invalid audio payload length");
        if((stream==MOZART_USB_STREAM_FAST64)!=l->negotiated) return ul_fail(l,"audio stream does not match negotiated block");
        if(capacity<block) return ul_fail(l,"audio output capacity is too small");
        if(l->have_seq && seq!=l->expected_seq) {
            if(seq==0 && !l->negotiated) { l->generation++; l->rx_frames=0; l->tx_frames=0; l->meta_head=0; l->meta_count=0; }
            else return ul_fail(l,"unexpected USB audio sequence");
        }
        l->expected_seq=seq+1; l->have_seq=true;
        if(l->meta_count==UL_META_CAP) return ul_fail(l,"audio metadata FIFO overflow");
        size_t at=(l->meta_head+l->meta_count)%UL_META_CAP; l->metadata[at]=(ul_meta_t){seq,ts}; l->meta_count++;
        memcpy(out,h+MOZART_USB_HEADER_BYTES,len); l->rx_frames++; result=block;
    }
    // 未知类型和流按长度跳过，后续帧继续使用同一个缓冲区。
    size_t consumed=MOZART_USB_HEADER_BYTES+(size_t)len;
    memmove(l->pending,l->pending+consumed,l->pending_len-consumed); l->pending_len-=consumed;
    return result<0?-1:result;
}

mozart_usblink_t *mozart_usblink_open(const char *dir)
{
    if(!dir) return NULL;
    mozart_usblink_t *l=calloc(1,sizeof(*l)); if(!l) return NULL; l->fd_out=l->fd_in=-1;
    char path[512]; int n=snprintf(path,sizeof(path),"%s/ep3",dir); if(n<0||(size_t)n>=sizeof(path)) { free(l); if(n>=0)errno=ENAMETOOLONG; return NULL; }
    l->fd_out=open(path,O_RDONLY|O_NONBLOCK); if(l->fd_out<0) { free(l); return NULL; }
    path[n-1]='4';
    l->fd_in=open(path,O_WRONLY|O_NONBLOCK); if(l->fd_in<0) { close(l->fd_out); free(l); return NULL; }
    if(ul_timer_init(l)<0) { int saved=errno; close(l->fd_out); close(l->fd_in); free(l); errno=saved; return NULL; }
    return l;
}
int mozart_usblink_read_audio(mozart_usblink_t *l,int16_t *stereo,int capacity)
{
    if(!l||!stereo) return -1;
    const uint64_t deadline=ul_deadline(UL_READ_TIMEOUT_MS);
    for(;;) {
        if(ul_now_us()>=deadline)return 0;
        size_t before=l->pending_len;
        int d=ul_decode(l,stereo,capacity,deadline); if(d<0)return -1; if(d>0)return d; if(l->stopped && !l->stop_ack_sent)return 0; if(l->pending_len!=before)continue;
        int ready=ul_wait(l->fd_out,POLLIN,deadline); if(ready<0)return ul_fail(l,"USB endpoint read poll failed"); if(ready==0)return 0;
        size_t room=sizeof(l->pending)-l->pending_len; if(room>UL_READ_CAP)room=UL_READ_CAP;
        if(ul_timer_arm(l,deadline)<0) return -1;
        ssize_t n=read(l->fd_out,l->pending+l->pending_len,room);
        int saved=errno;
        if(ul_timer_disarm(l)<0) return -1;
        errno=saved;
        if(n<0) { if(errno==EINTR||errno==EAGAIN||errno==EWOULDBLOCK)continue; return ul_fail(l,"USB endpoint read failed"); }
        if(n==0)return 0;
        l->pending_len+=(size_t)n;
    }
}
int mozart_usblink_write_audio(mozart_usblink_t *l,const int16_t *stereo,int frames)
{
    if(!l||!stereo)return -1;
    int block=l->negotiated?MOZART_USB_FAST64_SAMPLES:MOZART_USB_FAST_SAMPLES;
    if(frames!=block)return ul_fail(l,"output block size mismatch");
    if(!l->meta_count)return ul_fail(l,"audio metadata FIFO is empty");
    ul_meta_t meta=l->metadata[l->meta_head]; size_t bytes=(size_t)block*MOZART_USB_CHANNELS*MOZART_USB_SAMPLE_BYTES;
    unsigned char frame[MOZART_USB_HEADER_BYTES+MOZART_USB_FAST_BYTES];
    uint64_t ts=l->negotiated?meta.ts:ul_now_us(); ul_header(frame,MOZART_USB_TYPE_PCM,l->negotiated?MOZART_USB_STREAM_FAST64:MOZART_USB_STREAM_FAST,meta.seq,ts,(uint32_t)bytes); memcpy(frame+MOZART_USB_HEADER_BYTES,stereo,bytes);
    if(ul_write_all(l,frame,MOZART_USB_HEADER_BYTES+bytes,ul_deadline(MOZART_USB_IO_TIMEOUT_MS))<0)return -1;
    l->meta_head=(l->meta_head+1)%UL_META_CAP; l->meta_count--; l->tx_frames++; return frames;
}
int mozart_usblink_finish(mozart_usblink_t *l)
{
    if(!l)return -1;
    if(l->meta_count)return ul_fail(l,"STOP metadata is not drained");
    if(l->stop_ack_sent)return 0;
    unsigned char body[MOZART_USB_STOP_ACK_BYTES]; ul_put16(body,MOZART_USB_CONTROL_STOP_ACK); ul_put32(body+2,l->rx_stop_count); ul_put32(body+6,(uint32_t)l->tx_frames);
    if(ul_send_control(l,body,sizeof(body),ul_deadline(MOZART_USB_IO_TIMEOUT_MS))<0)return -1;
    l->stop_ack_sent=true;
    return 0;
}
uint64_t mozart_usblink_rx_frames(const mozart_usblink_t *l){return l?l->rx_frames:0;}
uint64_t mozart_usblink_tx_frames(const mozart_usblink_t *l){return l?l->tx_frames:0;}
int mozart_usblink_block_frames(const mozart_usblink_t *l){return l?(l->negotiated?MOZART_USB_FAST64_SAMPLES:MOZART_USB_FAST_SAMPLES):0;}
uint64_t mozart_usblink_generation(const mozart_usblink_t *l){return l?l->generation:0;}
bool mozart_usblink_stop_requested(const mozart_usblink_t *l){return l&&l->stopped;}
size_t mozart_usblink_pending_frames(const mozart_usblink_t *l){return l?l->meta_count:0;}
bool mozart_usblink_negotiated(const mozart_usblink_t *l){return l&&l->negotiated;}
const char *mozart_usblink_last_error(const mozart_usblink_t *l){return l?l->error:"invalid USB link";}
void mozart_usblink_close(mozart_usblink_t *l)
{
    if(!l)return;
    // close 无返回值；删除失败时将保存的 reason 输出到标准错误。
    if(timer_delete(l->io_timer)<0) {
        ul_timer_error(l,"delete");
        fprintf(stderr,"%s\n",l->error);
    }
    if(l->fd_out>=0)close(l->fd_out);
    if(l->fd_in>=0)close(l->fd_in);
    free(l);
}
