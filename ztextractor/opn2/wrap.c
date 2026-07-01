#include "ym3438.h"
int  opn2_size(void){ return (int)sizeof(ym3438_t); }
void opn2_reset(ym3438_t* c){ OPN2_Reset(c); }
void opn2_settype(int t){ OPN2_SetChipType((Bit32u)t); }
void opn2_wr(ym3438_t* c, int port, int addr, int data){
    Bit16s b[2]; int i;
    OPN2_Write(c, port,   addr); for(i=0;i<24;i++) OPN2_Clock(c,b);
    OPN2_Write(c, port|1, data); for(i=0;i<24;i++) OPN2_Clock(c,b);
}
/* one output sample = SUM of mol/mor over the 24-cycle frame (channels are time-muxed). */
void opn2_gen(ym3438_t* c, short* out, int n){
    Bit16s b[2]; int s,i; long l,r;
    for(s=0;s<n;s++){
        l=0; r=0;
        for(i=0;i<24;i++){ OPN2_Clock(c,b); l+=b[0]; r+=b[1]; }
        if(l>32767)l=32767; else if(l<-32768)l=-32768;
        if(r>32767)r=32767; else if(r<-32768)r=-32768;
        out[2*s]=(short)l; out[2*s+1]=(short)r;
    }
}
