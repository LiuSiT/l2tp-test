import argparse, asyncio, io, os, enum, struct, collections, hashlib, ipaddress, socket, random
import pproxy
from . import enums, message, crypto, ip, dns
from .__doc__ import *

class State(enum.Enum):
    INITIAL = 0
    SA_SENT = 1
    ESTABLISHED = 2
    DELETED = 3
    KE_SENT = 4
    HASH_SENT = 5
    AUTH_SET = 6
    CONF_SENT = 7
    CHILD_SA_SENT = 8

class ChildSa:
    def __init__(self, spi_in, spi_out, crypto_in, crypto_out):
        self.spi_in = spi_in
        self.spi_out = spi_out
        self.crypto_in = crypto_in
        self.crypto_out = crypto_out
        self.msgid_in = 1
        self.msgid_out = 1
        self.msgwin_in = set()
        self.tcp_stack = {}
        self.child = None
    def incr_msgid_in(self):
        self.msgid_in += 1
        while self.msgid_in in self.msgwin_in:
            self.msgwin_in.discard(self.msgid_in)
            self.msgid_in += 1

class IKEv1Session:
    def __init__(self, args, sessions, peer_spi):
        self.args = args
        self.sessions = sessions
        self.my_spi = os.urandom(8)
        self.peer_spi = peer_spi
        self.crypto = None
        self.my_nonce = os.urandom(32)
        self.peer_nonce = None
        self.state = State.INITIAL
        self.child_sa = []
        self.sessions[self.my_spi] = self
    def create_response(self, exchange, payloads, message_id=0, crypto=None):
        response = message.Message(self.peer_spi, self.my_spi, 0x10, exchange,
                enums.MsgFlag.NONE, message_id, payloads)
        print(repr(response))
        self.response_data.append(response.to_bytes(crypto=crypto))
    def process(self, request, stream):
        #if request.message_id == self.peer_msgid - 1:
        #    return self.response_data
        #elif request.message_id != self.peer_msgid:
        #    return
        request.parse_payloads(stream, crypto=self.crypto)
        print(repr(request))
        self.response_data = []
        if request.exchange == enums.Exchange.IKE_IDENTITY_1 and request.get_payload(enums.Payload.SA_1):
            assert self.state == State.INITIAL
            request_payload_sa = request.get_payload(enums.Payload.SA_1)
            self.sa_bytes = request_payload_sa.to_bytes()
            self.transform = request_payload_sa.proposals[0].transforms[0].values
            del request_payload_sa.proposals[0].transforms[1:]
            response_payloads = request.payloads
            self.create_response(enums.Exchange.IKE_IDENTITY_1, response_payloads)
            self.state = State.SA_SENT
        elif request.exchange == enums.Exchange.IKE_IDENTITY_1 and request.get_payload(enums.Payload.KE_1):
            assert self.state == State.SA_SENT
            self.peer_public_key = request.get_payload(enums.Payload.KE_1).ke_data
            self.my_public_key, self.shared_secret = crypto.DiffieHellman(self.transform[enums.TransformAttr.DH], self.peer_public_key)
            print(self.my_public_key, self.peer_public_key)
            self.peer_nonce = request.get_payload(enums.Payload.NONCE_1).nonce
            response_payloads = [ message.PayloadKE_1(self.my_public_key), message.PayloadNONCE_1(self.my_nonce),
                                  message.PayloadNATD_1(os.urandom(32)), message.PayloadNATD_1(os.urandom(32)) ]
            cipher = crypto.Cipher(self.transform[enums.TransformAttr.ENCR], self.transform[enums.TransformAttr.KEY_LENGTH])
            prf = crypto.Prf(self.transform[enums.TransformAttr.HASH])
            self.skeyid = prf.prf(self.args.passwd.encode(), self.peer_nonce+self.my_nonce)
            self.skeyid_d = prf.prf(self.skeyid, self.shared_secret+self.peer_spi+self.my_spi+bytes([0]))
            self.skeyid_a = prf.prf(self.skeyid, self.skeyid_d+self.shared_secret+self.peer_spi+self.my_spi+bytes([1]))
            self.skeyid_e = prf.prf(self.skeyid, self.skeyid_a+self.shared_secret+self.peer_spi+self.my_spi+bytes([2]))
            iv = prf.hasher(self.peer_public_key+self.my_public_key).digest()[:cipher.block_size]
            self.crypto = crypto.Crypto_1(cipher, self.skeyid_e[:cipher.key_size], iv, prf)
            self.create_response(enums.Exchange.IKE_IDENTITY_1, response_payloads)
            self.state = State.KE_SENT
        elif request.exchange == enums.Exchange.IKE_IDENTITY_1 and request.get_payload(enums.Payload.ID_1):
            assert self.state == State.KE_SENT
            payload_id = request.get_payload(enums.Payload.ID_1)
            prf = self.crypto.prf
            hash_i = prf.prf(self.skeyid, self.peer_public_key+self.my_public_key+self.peer_spi+self.my_spi+self.sa_bytes+payload_id.to_bytes())
            assert hash_i == request.get_payload(enums.Payload.HASH_1).data, 'Authentication Failed'
            response_payload_id = message.PayloadID_1(enums.IDType.ID_FQDN, self.args.userid.encode())
            hash_r = prf.prf(self.skeyid, self.my_public_key+self.peer_public_key+self.my_spi+self.peer_spi+self.sa_bytes+response_payload_id.to_bytes())
            response_payloads = [response_payload_id, message.PayloadHASH_1(hash_r)]
            #response_payloads.append(message.PayloadCP_1(enums.CFGType.CFG_REQUEST,
            #       {enums.CPAttrType.XAUTH_TYPE:b'\x01', enums.CPAttrType.XAUTH_USER_NAME: b'', enums.CPAttrType.XAUTH_USER_PASSWORD: b'', enums.CPAttrType.XAUTH_CHALLENGE: os.urandom(16)}))
            self.create_response(enums.Exchange.IKE_IDENTITY_1, response_payloads, crypto=self.crypto)
            self.crypto.lastblock = self.crypto.block
            self.state = State.HASH_SENT

            attrs = { enums.CPAttrType.XAUTH_TYPE: 0,
                      enums.CPAttrType.XAUTH_USER_NAME: b'',
                      enums.CPAttrType.XAUTH_USER_PASSWORD: b'',
                    }
            #attrs = { enums.CPAttrType.XAUTH_STATUS: 1 }
            message_id = random.randrange(1<<32)
            response_payloads = [message.PayloadCP_1(enums.CFGType.CFG_REQUEST, attrs)]
            buf = message.Message.encode_payloads(response_payloads)
            hash_r = self.crypto.prf.prf(self.skeyid_a, message_id.to_bytes(4, 'big') + buf)
            response_payloads.insert(0, message.PayloadHASH_1(hash_r))
            self.create_response(enums.Exchange.IKE_TRANSACTION_1, response_payloads, message_id=message_id, crypto=self.crypto)
        elif request.exchange == enums.Exchange.IKE_TRANSACTION_1:
            hash_i = self.crypto.prf.prf(self.skeyid_a, request.message_id.to_bytes(4, 'big') + message.Message.encode_payloads(request.payloads[1:]))
            assert hash_i == request.get_payload(enums.Payload.HASH_1).data
            payload_cp = request.get_payload(enums.Payload.CP_1)
            if enums.CPAttrType.XAUTH_USER_NAME in payload_cp.attrs:
                assert self.state == State.HASH_SENT
                response_payloads = [message.PayloadCP_1(enums.CFGType.CFG_SET, {enums.CPAttrType.XAUTH_STATUS: 1})]
                message_id = request.message_id
                self.state = State.AUTH_SET
            elif enums.CPAttrType.INTERNAL_IP4_ADDRESS in payload_cp.attrs:
                assert self.state == State.AUTH_SET
                attrs = { enums.CPAttrType.INTERNAL_IP4_ADDRESS: ipaddress.ip_address('1.0.0.1').packed,
                          enums.CPAttrType.INTERNAL_IP4_NETMASK: ipaddress.ip_address('1.0.0.255').packed,
                          enums.CPAttrType.INTERNAL_IP4_DNS: ipaddress.ip_address(self.args.dns).packed,
                        }
                response_payloads = [message.PayloadCP_1(enums.CFGType.CFG_REPLY, attrs, identifier=payload_cp.identifier)]
                message_id = request.message_id
                self.state = State.CONF_SENT
            else:
                raise Exception('Unknown CP Exchange')
            buf = message.Message.encode_payloads(response_payloads)
            hash_r = self.crypto.prf.prf(self.skeyid_a, message_id.to_bytes(4, 'big') + buf)
            response_payloads.insert(0, message.PayloadHASH_1(hash_r))
            self.create_response(enums.Exchange.IKE_TRANSACTION_1, response_payloads, message_id=message_id, crypto=self.crypto)
        elif request.exchange == enums.Exchange.IKE_QUICK_1:
            if len(request.payloads) == 1 and request.payloads[0].type == enums.Payload.HASH_1:
                assert self.state == State.QUICK_SENT
                self.state = State.ESTABLISHED
            else:
                assert self.state == State.CONF_SENT
                peer_nonce = request.get_payload(enums.Payload.NONCE_1).nonce
                my_nonce = os.urandom(len(peer_nonce))
                request.get_payload(enums.Payload.NONCE_1).nonce = my_nonce
                chosen_proposal = request.get_payload(enums.Payload.SA_1).proposals[0]
                del chosen_proposal.transforms[1:]
                peer_spi = chosen_proposal.spi
                chosen_proposal.spi = my_spi = os.urandom(4)
                response_payloads = request.payloads
                message_id = request.message_id
                buf = message.Message.encode_payloads(response_payloads[1:])
                hash_r = self.crypto.prf.prf(self.skeyid_a, message_id.to_bytes(4, 'big') + peer_nonce + buf)
                response_payloads[0] = message.PayloadHASH_1(hash_r)
                self.create_response(enums.Exchange.IKE_QUICK_1, response_payloads, message_id=message_id, crypto=self.crypto)
                self.state = State.QUICK_SENT
        else:
            raise Exception(f'unhandled request {request!r}')
        return self.response_data

class IKEv2Session:
    def __init__(self, args, sessions, peer_spi):
        self.args = args
        self.sessions = sessions
        self.my_spi = os.urandom(8)
        self.peer_spi = peer_spi
        self.peer_msgid = 0
        self.my_crypto = None
        self.peer_crypto = None
        self.my_nonce = os.urandom(random.randrange(16, 256))
        self.peer_nonce = None
        self.state = State.INITIAL
        self.request_data = None
        self.response_data = None
        self.child_sa = []
        self.sessions[self.my_spi] = self
    def create_key(self, ike_proposal, shared_secret, old_sk_d=None):
        prf = crypto.Prf(ike_proposal.get_transform(enums.Transform.PRF).id)
        integ = crypto.Integrity(ike_proposal.get_transform(enums.Transform.INTEG).id)
        cipher = crypto.Cipher(ike_proposal.get_transform(enums.Transform.ENCR).id,
                               ike_proposal.get_transform(enums.Transform.ENCR).keylen)
        if not old_sk_d:
            skeyseed = prf.prf(self.peer_nonce+self.my_nonce, shared_secret)
        else:
            skeyseed = prf.prf(old_sk_d, shared_secret+self.peer_nonce+self.my_nonce)
        keymat = prf.prfplus(skeyseed, self.peer_nonce+self.my_nonce+self.peer_spi+self.my_spi,
                             prf.key_size*3+integ.key_size*2+cipher.key_size*2)
        self.sk_d, sk_ai, sk_ar, sk_ei, sk_er, sk_pi, sk_pr = struct.unpack(
            '>{0}s{1}s{1}s{2}s{2}s{0}s{0}s'.format(prf.key_size, integ.key_size, cipher.key_size), keymat)
        self.my_crypto = crypto.Crypto(cipher, sk_er, integ, sk_ar, prf, sk_pr)
        self.peer_crypto = crypto.Crypto(cipher, sk_ei, integ, sk_ai, prf, sk_pi)
    def create_child_key(self, child_proposal, nonce_i, nonce_r):
        integ = crypto.Integrity(child_proposal.get_transform(enums.Transform.INTEG).id)
        cipher = crypto.Cipher(child_proposal.get_transform(enums.Transform.ENCR).id,
                               child_proposal.get_transform(enums.Transform.ENCR).keylen)
        keymat = self.my_crypto.prf.prfplus(self.sk_d, nonce_i+nonce_r, 2*integ.key_size+2*cipher.key_size)
        sk_ei, sk_ai, sk_er, sk_ar = struct.unpack('>{0}s{1}s{0}s{1}s'.format(cipher.key_size, integ.key_size), keymat)
        crypto_inbound = crypto.Crypto(cipher, sk_ei, integ, sk_ai)
        crypto_outbound = crypto.Crypto(cipher, sk_er, integ, sk_ar)
        child_sa = ChildSa(os.urandom(4), child_proposal.spi, crypto_inbound, crypto_outbound)
        self.child_sa.append(child_sa)
        self.sessions[child_sa.spi_in] = child_sa
        return child_sa
    def auth_data(self, message_data, nonce, payload, sk_p):
        prf = self.peer_crypto.prf.prf
        return prf(prf(self.args.passwd.encode(), b'Key Pad for IKEv2'), message_data+nonce+prf(sk_p, payload.to_bytes()))
    def create_response(self, exchange, payloads, crypto=None):
        response = message.Message(self.peer_spi, self.my_spi, 0x20, exchange,
                enums.MsgFlag.Response, self.peer_msgid, payloads)
        #print(repr(response))
        self.peer_msgid += 1
        self.response_data = response.to_bytes(crypto=crypto)
    def process(self, request, stream):
        if request.message_id == self.peer_msgid - 1:
            return [self.response_data]
        elif request.message_id != self.peer_msgid:
            return []
        request.parse_payloads(stream, crypto=self.peer_crypto)
        print(repr(request))
        if request.exchange == enums.Exchange.IKE_SA_INIT:
            assert self.state == State.INITIAL
            self.peer_nonce = request.get_payload(enums.Payload.NONCE).nonce
            chosen_proposal = request.get_payload(enums.Payload.SA).get_proposal(enums.EncrId.ENCR_AES_CBC)
            payload_ke = request.get_payload(enums.Payload.KE)
            public_key, shared_secret = crypto.DiffieHellman(payload_ke.dh_group, payload_ke.ke_data)
            self.create_key(chosen_proposal, shared_secret)
            #checksum_i1 = hashlib.sha1(self.peer_spi+self.my_spi+ipaddress.ip_address(self.peer_addr[0]).packed+self.peer_addr[1].to_bytes(2, 'big')).digest()
            #checksum_i2 = hashlib.sha1(self.peer_spi+self.my_spi+ipaddress.ip_address(self.my_addr[0]).packed+self.my_addr[1].to_bytes(2, 'big')).digest()
            # send wrong checksum to make sure NAT enabled
            response_payloads = [ message.PayloadSA([chosen_proposal]),
                                  message.PayloadNONCE(self.my_nonce),
                                  message.PayloadKE(payload_ke.dh_group, public_key),
                                  message.PayloadVENDOR(f'{__title__}-{__version__}'.encode()),
                                  message.PayloadNOTIFY(0, enums.Notify.NAT_DETECTION_DESTINATION_IP, b'', os.urandom(20)),
                                  message.PayloadNOTIFY(0, enums.Notify.NAT_DETECTION_SOURCE_IP, b'', os.urandom(20)) ]
            self.create_response(enums.Exchange.IKE_SA_INIT, response_payloads)
            self.state = State.SA_SENT
            self.request_data = stream.getvalue()
        elif request.exchange == enums.Exchange.IKE_AUTH:
            assert self.state == State.SA_SENT
            request_payload_idi = request.get_payload(enums.Payload.IDi)
            request_payload_auth = request.get_payload(enums.Payload.AUTH)
            if request_payload_auth is None:
                EAP = True
                raise Exception('EAP not supported')
            else:
                EAP = False
                auth_data = self.auth_data(self.request_data, self.my_nonce, request_payload_idi, self.peer_crypto.sk_p)
                assert auth_data == request_payload_auth.auth_data, 'Authentication Failed'
            chosen_child_proposal = request.get_payload(enums.Payload.SA).get_proposal(enums.EncrId.ENCR_AES_CBC)
            child_sa = self.create_child_key(chosen_child_proposal, self.peer_nonce, self.my_nonce)
            chosen_child_proposal.spi = child_sa.spi_in
            response_payload_idr = message.PayloadIDr(enums.IDType.ID_FQDN, self.args.userid.encode())
            auth_data = self.auth_data(self.response_data, self.peer_nonce, response_payload_idr, self.my_crypto.sk_p)

            response_payloads = [ message.PayloadSA([chosen_child_proposal]),
                                  request.get_payload(enums.Payload.TSi),
                                  request.get_payload(enums.Payload.TSr),
                                  response_payload_idr,
                                  message.PayloadAUTH(enums.AuthMethod.PSK, auth_data) ]
            if request.get_payload(enums.Payload.CP):
                attrs = { enums.CPAttrType.INTERNAL_IP4_ADDRESS: ipaddress.ip_address('1.0.0.1').packed,
                          enums.CPAttrType.INTERNAL_IP4_NETMASK: ipaddress.ip_address('1.0.0.255').packed,
                          enums.CPAttrType.INTERNAL_IP4_DNS: ipaddress.ip_address(self.args.dns).packed, }
                response_payloads.append(message.PayloadCP(enums.CFGType.CFG_REPLY, attrs))
            self.create_response(enums.Exchange.IKE_AUTH, response_payloads, self.my_crypto)
            self.state = State.ESTABLISHED
        elif request.exchange == enums.Exchange.INFORMATIONAL:
            assert self.state == State.ESTABLISHED
            response_payloads = []
            delete_payload = request.get_payload(enums.Payload.DELETE)
            if not request.payloads:
                pass
            elif delete_payload and delete_payload.protocol == enums.Protocol.IKE:
                self.state = State.DELETED
                self.sessions.pop(self.my_spi)
                for child_sa in self.child_sa:
                    self.sessions.pop(child_sa.spi_in)
                self.child_sa = []
                response_payloads.append(delete_payload)
            elif delete_payload:
                spis = []
                for spi in delete_payload.spis:
                    child_sa = next((x for x in self.child_sa if x.spi_out == spi), None)
                    if child_sa:
                        self.child_sa.remove(child_sa)
                        self.sessions.pop(child_sa.spi_in)
                        spis.append(child_sa.spi_in)
                response_payloads.append(message.PayloadDELETE(delete_payload.protocol, spis))
            else:
                raise Exception(f'unhandled informational {request!r}')
            self.create_response(enums.Exchange.INFORMATIONAL, response_payloads, self.my_crypto)
        elif request.exchange == enums.Exchange.CREATE_CHILD_SA:
            assert self.state == State.ESTABLISHED
            chosen_proposal = request.get_payload(enums.Payload.SA).get_proposal(enums.EncrId.ENCR_AES_CBC)
            if chosen_proposal.protocol != enums.Protocol.IKE:
                payload_notify = next((i for i in request.get_payloads(enums.Payload.NOTIFY) if i.notify==enums.Notify.REKEY_SA), None)
                if not payload_notify:
                    raise Exception(f'unhandled protocol {chosen_proposal.protocol} {request!r}')
                old_child_sa = next(i for i in self.child_sa if i.spi_out == payload_notify.spi)
                peer_nonce = request.get_payload(enums.Payload.NONCE).nonce
                my_nonce = os.urandom(random.randrange(16, 256))
                child_sa = self.create_child_key(chosen_proposal, peer_nonce, my_nonce)
                chosen_proposal.spi = child_sa.spi_in
                child_sa.tcp_stack = old_child_sa.tcp_stack
                old_child_sa.child = child_sa
                response_payloads = [ message.PayloadNOTIFY(chosen_proposal.protocol, enums.Notify.REKEY_SA, old_child_sa.spi_in, b''),
                                      message.PayloadNONCE(my_nonce),
                                      message.PayloadSA([chosen_proposal]),
                                      request.get_payload(enums.Payload.TSi),
                                      request.get_payload(enums.Payload.TSr) ]
            else:
                child = IKEv2Session(self.args, self.sessions, chosen_proposal.spi)
                child.state = State.ESTABLISHED
                child.peer_nonce = request.get_payload(enums.Payload.NONCE).nonce
                child.child_sa = self.child_sa
                self.child_sa = []
                payload_ke = request.get_payload(enums.Payload.KE)
                public_key, shared_secret = crypto.DiffieHellman(payload_ke.dh_group, payload_ke.ke_data)
                chosen_proposal.spi = child.my_spi
                child.create_key(chosen_proposal, shared_secret, self.sk_d)
                response_payloads = [ message.PayloadSA([chosen_proposal]),
                                      message.PayloadNONCE(child.my_nonce),
                                      message.PayloadKE(payload_ke.dh_group, public_key) ]
            self.create_response(enums.Exchange.CREATE_CHILD_SA, response_payloads, self.my_crypto)
        else:
            raise Exception(f'unhandled request {request!r}')
        return [self.response_data]

IKE_HEADER = b'\x00\x00\x00\x00'

class IKE_500(asyncio.DatagramProtocol):
    def __init__(self, args, sessions):
        self.args = args
        self.sessions = sessions
    def connection_made(self, transport):
        self.transport = transport
    def datagram_received(self, data, addr, *, response_header=b''):
        stream = io.BytesIO(data)
        request = message.Message.parse(stream)
        if request.exchange == enums.Exchange.IKE_SA_INIT:
            session = IKEv2Session(self.args, self.sessions, request.spi_i)
        elif request.exchange == enums.Exchange.IKE_IDENTITY_1 and request.spi_r == bytes(8):
            session = IKEv1Session(self.args, self.sessions, request.spi_i)
        else:
            session = self.sessions.get(request.spi_r)
            if session is None:
                return
        for response in session.process(request, stream):
            self.transport.sendto(response_header+response, addr)

class SPE_4500(IKE_500):
    def datagram_received(self, data, addr):
        spi = data[:4]
        if spi == b'\xff':
            self.transport.sendto(b'\xff', addr)
        elif spi == IKE_HEADER:
            IKE_500.datagram_received(self, data[4:], addr, response_header=IKE_HEADER)
        elif spi in self.sessions:
            seqnum = int.from_bytes(data[4:8], 'big')
            sa = self.sessions[spi]
            if seqnum < sa.msgid_in or seqnum in sa.msgwin_in:
                return
            sa.crypto_in.verify_checksum(data)
            if seqnum > sa.msgid_in + 65536:
                sa.incr_msgid_in()
            if seqnum == sa.msgid_in:
                sa.incr_msgid_in()
            else:
                sa.msgwin_in.add(seqnum)
            header, data = sa.crypto_in.decrypt_esp(data[8:])
            def reply(data):
                nonlocal sa
                while sa and sa.spi_in not in self.sessions:
                    sa = sa.child
                if not sa:
                    return False
                encrypted = bytearray(sa.crypto_out.encrypt_esp(header, data))
                encrypted[0:0] = sa.spi_out + sa.msgid_out.to_bytes(4, 'big')
                sa.crypto_out.add_checksum(encrypted)
                sa.msgid_out += 1
                self.transport.sendto(encrypted, addr)
                return True
            if header == enums.IpProto.IPV4:
                proto, src_ip, dst_ip, ip_body = ip.parse_ipv4(data)
                if proto == enums.IpProto.UDP:
                    src_port, dst_port, udp_body = ip.parse_udp(ip_body)
                    print(f'IPv4 UDP {src_ip}:{src_port} -> {dst_ip}:{dst_port}', len(udp_body))
                    if dst_port == 53:
                        record = dns.DNSRecord.unpack(udp_body)
                        print('IPv4 UDP/DNS Query', record.q.qname)
                    def udp_reply(udp_body):
                        #print(f'IPv4 UDP Reply {dst_ip}:{dst_port} -> {src_ip}:{src_port}', result)
                        if dst_port == 53:
                            record = dns.DNSRecord.unpack(udp_body)
                            print('IPv4 UDP/DNS Result', ' '.join(f'{r.rname}->{r.rdata}' for r in record.rr))
                        ip_body = ip.make_udp(dst_port, src_port, udp_body)
                        data = ip.make_ipv4(proto, dst_ip, src_ip, ip_body)
                        reply(data)
                    asyncio.ensure_future(self.args.urserver.udp_sendto(str(dst_ip), dst_port, udp_body, udp_reply, (str(src_ip), src_port)))
                elif proto == enums.IpProto.TCP:
                    src_port, dst_port, flag, tcp_body = ip.parse_tcp(ip_body)
                    if flag & 2:
                        print(f'IPv4 TCP {src_ip}:{src_port} -> {dst_ip}:{dst_port} CONNECT')
                    #else:
                    #    print(f'IPv4 TCP {src_ip}:{src_port} -> {dst_ip}:{dst_port}', ip_body)
                    key = (str(src_ip), src_port)
                    if key not in sa.tcp_stack:
                        for spi, tcp in list(sa.tcp_stack.items()):
                            if tcp.obsolete():
                                sa.tcp_stack.pop(spi)
                        sa.tcp_stack[key] = tcp = ip.TCPStack(src_ip, src_port, dst_ip, dst_port, reply, self.args.rserver)
                    else:
                        tcp = sa.tcp_stack[key]
                    tcp.parse(ip_body)
                else:
                    print('IPv4', enums.IpProto(proto).name, src_ip, '->', dst_ip, data)
            else:
                print(enums.IpProto(header).name, data)
        else:
            print('unknown packet', data, addr)

DIRECT = pproxy.Connection('direct://')

def main():
    parser = argparse.ArgumentParser(description=__description__, epilog=f'Online help: <{__url__}>')
    parser.add_argument('-r', dest='rserver', default=DIRECT, type=pproxy.Connection, help='tcp remote server uri (default: direct)')
    parser.add_argument('-ur', dest='urserver', default=DIRECT, type=pproxy.Connection, help='udp remote server uri (default: direct)')
    parser.add_argument('-i', dest='userid', default='test', help='userid (default: test)')
    parser.add_argument('-p', dest='passwd', default='test', help='password (default: test)')
    parser.add_argument('-dns', dest='dns', default='8.8.8.8', help='dns server (default: 8.8.8.8)')
    parser.add_argument('-v', dest='v', action='count', help='print verbose output')
    parser.add_argument('--version', action='version', version=f'{__title__} {__version__}')
    args = parser.parse_args()
    loop = asyncio.get_event_loop()
    sessions = {}
    transport1, _ = loop.run_until_complete(loop.create_datagram_endpoint(lambda: IKE_500(args, sessions), ('0.0.0.0', 500)))
    transport2, _ = loop.run_until_complete(loop.create_datagram_endpoint(lambda: SPE_4500(args, sessions), ('0.0.0.0', 4500)))
    print('Serving on UDP :500 :4500...')
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        print('exit')
    for task in asyncio.Task.all_tasks():
        task.cancel()
    transport1.close()
    transport2.close()
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.close()

if __name__ == '__main__':
    main()
