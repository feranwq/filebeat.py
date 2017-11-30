#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
使用Python简单实现filebeat(https://www.elastic.co/products/beats/filebeat)逻辑,推送数据到下游

Authors: iyaozhen

Date: 2016-04-20

Since 1.1:
增加子进程 shell 命令"注释"，防止别人误杀进程
Since 1.2:
增加beat.hostname、beat.ip、host字段

Date: 2017-11-30
Authors: feran
根据公司业务修改添加部分模块
"""

import socket
import time
import json
import subprocess
import select
import random
import sys
import os
import logging
import logging.handlers


class FileBeat(object):
    """
    py filebeat 类
    """
    @classmethod
    def publish_to_logstash(cls, sockets, data, fields=None, timeout=10):
        """
        发布数据到logstash
        随机选取下游节点
        发出去的是个json格式数据包, 方便设置一些自定义字段, logtash接收数据时配置:
        input {
            tcp {
                port => xxxx
                codec => json
            }
        }
        Args:
            sockets: 已经和logstash建立的长连接dict
            data: 需要推送的数据
            fields: 自定义字段
            timeout: 某个logtash节点挂掉超时等待时间

        Returns:
            如果成功返回已发送内容的字节大小,失败返回False
        """
        # 没有可用通道
        if sockets is False:
            return False
        else:
            # 数据封装
            # packet_data = {
            #     'message': data.encode('utf8', 'ignore')
            # }
            # 添加自定义字段
            if fields is not None:
                for key, value in fields.iteritems():
                    data[key] = value
            packet_data = json.dumps(data) + '\r\n'

            # 随机选取出一个有效的socket通道
            random_address = cls.__random_choice_socket(sockets)
            try:
                if random_address is False:
                    # 没有可用通道, 手动抛出异常, 保证捕获异常后有一次重连机会
                    raise socket.error
                else:
                    res = sockets[random_address].sendall(packet_data)
            except socket.error:
                if random_address is not False:
                    # 将出错的socket置为False
                    sockets[random_address] = False
                # 等待, 然后尝试重连
                time.sleep(timeout)
                cls.re_connect(sockets)
                # 所有的通道都已经挂了则直接返回发送失败(本次发布操作不再进行重试), 防止进程间管道阻塞
                # 因此logstash集群挂掉期间的数据会丢失
                if cls.is_all_fail(sockets):
                    return False
                else:
                    # 尝试重新发送
                    return cls.publish_to_logstash(sockets, data, fields)
            else:
                return res

    @staticmethod
    def get_socket(address):
        """
        根据配置信息建立tcp连接
        Args:
            address: host:name字符串

        Returns:
            建立成功返回socket对象, 失败返回False
        """
        (ip, port) = address.split(':')
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)  # 在客户端开启心跳维护
        try:
            s.connect((ip, int(port)))
        except socket.error:
            return False
        else:
            return s

    @classmethod
    def get_sockets(cls, addresses):
        """
        根据一批配置信息建立tcp连接
        Args:
            addresses: host:name的list

        Returns:
            返回每个addresse对应的socket(字典), 全部连接失败返回False
        """
        sockets = {}
        for address in addresses:
            sockets[address] = cls.get_socket(address)
        # 建立连接都没有成功
        if cls.is_all_fail(sockets):
            return False
        else:
            return sockets

    @classmethod
    def re_connect(cls, sockets):
        """
        重建连接
        Args:
            sockets: socket dict

        Returns:
            None
        """
        for address, socket in sockets.iteritems():
            if socket is False:
                sockets[address] = cls.get_socket(address)

    @staticmethod
    def is_all_fail(sockets):
        """
        检查已经建立连接的sockets是否都挂了
        Args:
            sockets: socket dict

        Returns:
            bool
        """
        for socket in sockets.values():
            if socket is not False:
                return False

        return True

    @staticmethod
    def __random_choice_socket(sockets):
        real_sockets = {}
        for address, socket in sockets.iteritems():
            if socket is not False:
                real_sockets[address] = socket

        if real_sockets:
            return random.choice(real_sockets.keys())
        else:
            return False

    @staticmethod
    def mysql_to_redis(mysql_conn=None, redis_conn=None):
        """
        传入mysql连接和redis连接,根据mysql查询到的ip与mac对应关系,同步到redis数据库
        KEY为IP,VALUE为MAC,过期时间14400秒
        Args:
            mysql_conn:
            redis_conn:
        :return: bool
        """
        if mysql_conn and redis_conn:
            with mysql_conn.cursor() as cursor:
                # Create a new record
                sql = "SELECT inet_ntoa(address),hex(hwaddr),expire,count(distinct address) from lease4 where expire>(%s) group by address"
                cursor.execute(sql, (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                result = cursor.fetchall()
                for i in result:
                    redis_conn.set(i['inet_ntoa(address)'], i['hex(hwaddr)'])
                    redis_conn.expire(i['inet_ntoa(address)'], 14400)

    @classmethod
    def data_insert_usermac(cls, data_json, mysql_conn=None, redis_conn=None):
        """
        json解析后的data传进来,根据data['userip']取到MAC后添加字段data['usermac']=usermac
        如果没能取到MAC重试2次,都失败的话此ip暂时拉黑填入MAC AAAAAAAAAAAA,过期时间3600秒
        Args:
            data_json:data_jsonlog()预处理过的data
            mysql_conn:
            redis_conn:redis连接
        Returns:
            data:添加过'usermac'字段后的data
        """
        try:
            userip = data_json['userip']
            for i in xrange(2):
                usermac = redis_conn.get(userip)
                if usermac:
                    data_json['usermac'] = usermac
                else:
                    cls.mysql_to_redis(mysql_conn, redis_conn)
            else:
                logging.warning("can not get usermac from redis,user ip %s, set user mac AAAAAAAAAAAA" % userip)
                redis_conn.set(userip, "AAAAAAAAAAAA")
                redis_conn.expire(userip, 3600)
        except Exception, e:
            logging.warning("user mac insert faild,please check #publish_to_logstash() %s" % e)
            sys.exit()


    @staticmethod
    def data_kea_yuchuli(rawdata):
        re_kea = r'(\d+-\d+-\d+ \d+:\d+:\d+\.\d+) .+? \[.*\] DHCP4_PACKET_([A-Z]{4,}) \[hwtype=1 ([a-z|0-9|:].+)\], cid=*.+ (DHCP[A-Z].+) \(.*\) .*from ([0-9|\.].+) to ([0-9|\.].+) on interface .*'
        red_kea = re.match(re_kea, rawdata)
        return json.dumps({'time': red_kea.group(1), 'action': red_kea.group(2), 'keatype': red_kea.group(3),
                           'usermac': red_kea.group(4), 'destination': red_kea.group(6), 'source': red_kea.group(5)})
    @staticmethod
    def data_dnsmasq_yuchuli(rawdata):
        re_dns = r'([A-Z].+:\d+) .*\] ([a-z].*) from ((\d+.){3}(\d+))'
        red_dns = re.match(re_dns, rawdata)
        return json.dumps({'time': red_dns.group(1), 'userip': red_dns.group(3), 'querydomain': red_dns.group(2)})

    @staticmethod
    def data_jsonlog(json_data):
        """
        原始日志直接为json或者经过处理后成为json格式的日志数据传进来进行预处理
        re修复特殊字符bug,并且进行json解析返回
        可以直接传进来的日志:Nginx:access.log, Tomcat:portal.log
        Args:
            json_data:已经是json格式但还没有json.loads的data
        Returns:
            data:json解析后的data,type为字典
        """
        regex = re.compile(r'\\(?![/u"])')
        fixed = regex.sub(r"\\\\", json_data)
        data = json.loads(fixed)
        return data



    @classmethod
    def log_type_switch(cls, logtype, rawdata):
        """
        根据filebeat.json里的logtype来执行不同日志格式的处理,完成后返回处理好的json格式日志
        Args:
            logtype:
            rawdata:
        :return: 
        """
        switch = {
            "kea": cls.data_kea_yuchuli(rawdata),
            "dnsmasq": cls.data_dnsmasq_yuchuli(rawdata),
            "nginx": rawdata,
            "tomcat": rawdata
        }
        return data_jsonlog(switch[logtype])



    @staticmethod
    def __list_in_string(search, string):
        """
        判断list中的某个元素是否在字符串中
        Args:
            search: list, 需要去string中查找的字符串集
            string: 字符串

        Returns:
            bool
        """
        return any(temp in string for temp in search)

    @classmethod
    def data_filter(cls, data, include_lines=None, exclude_lines=None):
        """
        数据过滤器, 检测数据是否可以通过过滤
        Args:
            data: 获取到的数据
            include_lines: 需要包含的行
            exclude_lines: 需要排除的行

        Returns:
            bool
        """
        if include_lines is not None:
            # 有白名单则需至少命中一个才能通过
            if cls.__list_in_string(include_lines, data):
                if exclude_lines is not None:
                    # 黑名单命中一个就不能通过
                    if cls.__list_in_string(exclude_lines, data):
                        return False
                    else:
                        return True
                else:
                    return True
            else:
                return False
        else:
            if exclude_lines is not None:
                if cls.__list_in_string(exclude_lines, data):
                    return False
                else:
                    return True
            else:
                return True

    @staticmethod
    def tail_file(file_path, from_head=False):
        """
        创建子进程tail file
        Args:
            file_path: 文件路径
            from_head: 是否重头开始读取文件

        Returns:

        """
        # 安全注释, 防止OP不小心kill子进程
        safe_comment = "'(Do not kill me, parent PID: %d)'" % os.getpid()
        if from_head is True:
            # 先输出现有文件全部内容, 然后tail文件
            # -F 当文件变化时能切换到新的文件
            cmd = "cat %s && tail -F %s %s" % (file_path, file_path, safe_comment)
        else:
            cmd = "tail -F %s %s" % (file_path, safe_comment)

        try:
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, shell=True, bufsize=-1)
        except OSError as e:
            return False, str(e)
        else:
            # https://docs.python.org/2/library/select.html
            poll = select.epoll()
            poll.register(process.stdout)

            return process, poll

    @staticmethod
    def get_current_path(file_path, file_ext):
        """
        获取当前文件路径
        Args:
            file_path: 文件基础路径
            file_ext: 文件后缀(时间戳参数)

        Returns:
            string
        """
        if file_ext is not None:
            return file_path % time.strftime(file_ext, time.localtime())
        else:
            return file_path

    @staticmethod
    def is_non_zero_file(file_path):
        """
        检查文件是否存在且不为空
        Args:
            file_path: 文件路径

        Returns:
            bool
        """
        return True if os.path.isfile(file_path) and os.path.getsize(file_path) > 0 \
            else False

    @staticmethod
    def init_log(log_path, level=logging.INFO, when="D", backup=7,
                 log_format="%(levelname)s: %(asctime)s: %(filename)s:%(lineno)d * %(thread)d %(message)s",
                 datefmt="%m-%d %H:%M:%S"):
        """
        init_log - initialize log module

        Args:
          log_path:      - Log file path prefix.
                           Log data will go to two files: log_path.log and log_path.log.wf
                           Any non-exist parent directories will be created automatically
          level:         - msg above the level will be displayed
                           DEBUG < INFO < WARNING < ERROR < CRITICAL
                           the default value is logging.INFO
          when:          - how to split the log file by time interval
                           'S' : Seconds
                           'M' : Minutes
                           'H' : Hours
                           'D' : Days
                           'W' : Week day
                           default value: 'D'
          backup:        - how many backup file to keep
                           default value: 7
          log_format:    - format of the log
                           default format:
                           %(levelname)s: %(asctime)s: %(filename)s:%(lineno)d * %(thread)d %(message)s
                           INFO: 12-09 18:02:42: log.py:40 * 139814749787872 HELLO WORLD
          datefmt:        - log date format

        Raises:
            OSError: fail to create log directories
            IOError: fail to open log file

        Returns:
            None
        """
        formatter = logging.Formatter(log_format, datefmt)
        logger = logging.getLogger()
        logger.setLevel(level)

        log_dir = os.path.dirname(log_path)
        if not os.path.isdir(log_dir):
            os.makedirs(log_dir)

        handler = logging.handlers.TimedRotatingFileHandler(log_path + ".log",
                                                            when=when,
                                                            backupCount=backup)
        handler.setLevel(level)
        handler.setFormatter(formatter)
        logger.addHandler(handler)

        handler = logging.handlers.TimedRotatingFileHandler(log_path + ".log.wf",
                                                            when=when,
                                                            backupCount=backup)
        handler.setLevel(logging.WARNING)
        handler.setFormatter(formatter)
        logger.addHandler(handler)


def run():
    """
    运行任务
    Returns:

    """
    FileBeat.init_log("./filebeat", logging.DEBUG)

    try:
        conf_file = sys.argv[1]
    except IndexError:
        logging.warning("use default configure file: filebeat.json")
        conf_file = "filebeat.json"

    with open(conf_file, 'r') as f:
        conf = json.load(f)

    file_path = conf['filebeat']['path']
    file_date_ext = conf['filebeat']['date_ext']
    include_lines = conf['filebeat']['include_lines']
    exclude_lines = conf['filebeat']['exclude_lines']
    encoding = conf['filebeat']['encoding']
    logstash_hosts = conf['logstash']['hosts']
    from_head = conf['filebeat']['from_head'] if 'from_head' in conf['filebeat'] else True
    fields = conf['filebeat']['fields']

    logtype = conf['filebeat']['logtype']

    mysql_config = {
        'user': conf['mysql']['user'],
        'password': conf['mysql']['password'],
        'host': conf['mysql']['host'],
        'db': conf['mysql']['db'],
        'charset': conf['mysql']['charset'],
        'cursorclass': pymysql.cursors.DictCursor
    }
    redis_config = {
        'host': conf['redis']['host'],
        'port': conf['redis']['prot'],
        'db': conf['redis']['db'],
        'password': conf['redis']['password'],
    }
    mysql_conn = pymysql.connect(**mysql_config)
    redis_conn = redis.StrictRedis(**redis_config)
    # beat_hostname = socket.gethostname()
    # try:
    #     ip_address = subprocess.check_output(["hostname", "-i"]).rstrip()
    # except subprocess.CalledProcessError:
    #     ip_address = beat_hostname
    # # 高版本系统使用 -I 参数获取本机ip
    # if ip_address == beat_hostname or ip_address == '127.0.0.1' or ip_address == 'localhost':
    #     try:
    #         ip_address = subprocess.check_output(["hostname", "-I"]).rstrip()
    #     except subprocess.CalledProcessError:
    #         pass
    # fields['beat.hostname'] = beat_hostname
    # fields['beat.ip'] = ip_address
    # fields['host'] = beat_hostname

    sockets = FileBeat.get_sockets(logstash_hosts)
    if sockets is False:
        sys.exit("error: can not connect logstash clusters")
    else:
        current_file_path = FileBeat.get_current_path(file_path, file_date_ext)
        last_file_path = current_file_path
        while True:
            # 如果文件不存在, 等待当前文件生成
            while FileBeat.is_non_zero_file(current_file_path) is False:
                logging.info("waiting file %s create" % current_file_path)
                time.sleep(60)
                current_file_path = FileBeat.get_current_path(file_path, file_date_ext)
            # 创建子进程tail文件
            logging.info("start tail file " + current_file_path)
            (process, poll) = FileBeat.tail_file(current_file_path, from_head)
            if process is False:
                error_str = poll
                logging.error(error_str)
            # 轮训子进程是否获取到数据
            while True:
                if poll.poll(1):  # timeout 1s
                    data = process.stdout.readline().rstrip()
                    # 统一转换为unicode编码
                    data_unicode = data.decode(encoding, 'ignore')
                    if FileBeat.data_filter(data_unicode, include_lines, exclude_lines):
                        data_json = FileBeat.log_type_switch(logtype, data_unicode)
                        if 'usermac' not in data_json.keys():
                            FileBeat.data_insert_usermac(data_json, mysql_conn, redis_conn)
                        if FileBeat.publish_to_logstash(sockets, data_json, fields) is False:
                            logging.error("publish to logstash fail [%s]" % data)
                        else:
                            logging.info("publish to logstash success")
                else:
                    # 若当前目标日志文件名变化, 则跳出循环, 读取新的文件
                    current_file_path = FileBeat.get_current_path(file_path, file_date_ext)
                    if current_file_path != last_file_path:
                        poll.unregister(process.stdout)
                        process.kill()
                        last_file_path = current_file_path
                        break


if __name__ == "__main__":
    run()
