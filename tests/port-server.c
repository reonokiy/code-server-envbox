/* Static, synthetic TCP/UDP server: no external images or credentials. */
#include <arpa/inet.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <unistd.h>
#include <string.h>

int main(void) {
    int tcp = socket(AF_INET, SOCK_STREAM, 0);
    int udp = socket(AF_INET, SOCK_DGRAM, 0);
    int reuse = 1;
    struct sockaddr_in address = {.sin_family = AF_INET, .sin_port = htons(3001),
                                 .sin_addr.s_addr = INADDR_ANY};
    setsockopt(tcp, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));
    if (bind(tcp, (void *)&address, sizeof(address)) ||
        bind(udp, (void *)&address, sizeof(address)) || listen(tcp, 8)) return 1;
    for (;;) {
        fd_set ready;
        FD_ZERO(&ready); FD_SET(tcp, &ready); FD_SET(udp, &ready);
        if (select((tcp > udp ? tcp : udp) + 1, &ready, 0, 0, 0) < 0) return 2;
        if (FD_ISSET(tcp, &ready)) {
            int client = accept(tcp, 0, 0);
            char request[4096];
            read(client, request, sizeof(request));
            const char *response = "HTTP/1.1 200 OK\r\nContent-Length: 12\r\nConnection: close\r\n\r\ndocker-proof";
            write(client, response, strlen(response)); close(client);
        }
        if (FD_ISSET(udp, &ready)) {
            char data[4096]; struct sockaddr_in peer; socklen_t size = sizeof(peer);
            ssize_t count = recvfrom(udp, data, sizeof(data), 0, (void *)&peer, &size);
            if (count >= 0) sendto(udp, data, count, 0, (void *)&peer, size);
        }
    }
}
