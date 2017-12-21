# coding: -*- utf-8 -*-
"""
日志生成器,指定一个源文件,根据定义大小循环输出到新文件
"""
import os
import sys

def logGenerator(filename):
     with open(filename, 'r') as f:
         while True:
             try:
                 yield next(f)
             except:
                 f.seek(0)

def logCreator(filename, size):
    dx = 0
    newfile = '500'+filename
    log = logGenerator(filename)
    with open(newfile, 'a') as f:
        while (dx < size):
            f.write(next(log))
            dx = os.path.getsize(newfile)
if __name__ == "__main__":
    filename = sys.argv[1]
    logCreator(filename, 1024*1024*500)