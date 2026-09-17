#include "pm/v7_hot_signal.hpp"
#include <cassert>
#include <filesystem>
#include <cstring>
#include <iostream>
#include <poll.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

int main(){
    auto path=std::filesystem::temp_directory_path()/("v7-hot-signal-"+std::to_string(::getpid())+".sock");
    int fd=::socket(AF_UNIX,SOCK_DGRAM,0);assert(fd>=0);sockaddr_un a{};a.sun_family=AF_UNIX;
    auto text=path.string();std::memcpy(a.sun_path,text.c_str(),text.size()+1);::unlink(text.c_str());
    assert(::bind(fd,reinterpret_cast<sockaddr*>(&a),sizeof(a))==0);
    pm::v7::HotSignalDatagramSender sender(path);assert(sender.send("{\"version\":7}"));
    pollfd p{fd,POLLIN,0};assert(::poll(&p,1,1000)==1);char buffer[128]{};auto n=::recv(fd,buffer,sizeof(buffer),0);
    assert(n==13);assert(std::string_view(buffer,n)=="{\"version\":7}");assert(sender.sent()==1);assert(sender.errors()==0);
    ::close(fd);::unlink(text.c_str());std::cout<<"hot signal datagram PASS\n";
}
