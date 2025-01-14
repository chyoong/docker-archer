# -*- coding: UTF-8 -*- 

import re
import json
import datetime
import multiprocessing

from django.db.models import Q
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts import render, get_object_or_404
from django.http import HttpResponse, HttpResponseRedirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import check_password

from .dao import Dao
from .const import Const
from .inception import InceptionDao
from .aes_decryptor import Prpcrypt
from .models import users, master_config, workflow

dao = Dao()
inceptionDao = InceptionDao()
prpCryptor = Prpcrypt()
login_failure_counter = {} #登录失败锁定计数器，给loginAuthenticate用的
sqlSHA1_cache = {} #存储SQL文本与SHA1值的对应关系，尽量减少与数据库的交互次数,提高效率。格式: {工单ID1:{SQL内容1:sqlSHA1值1, SQL内容2:sqlSHA1值2},}

#ajax接口，登录页面调用，用来验证用户名密码
@csrf_exempt
def loginAuthenticate(username, password):
    """登录认证，包含一个登录失败计数器，5分钟内连续失败5次的账号，会被锁定5分钟"""
    lockCntThreshold = 5
    lockTimeThreshold = 300

    #服务端二次验证参数
    strUsername = username
    strPassword = password
    if strUsername == "" or strPassword == "" or strUsername is None or strPassword is None:
        result = {'status':2, 'msg':'登录用户名或密码为空，请重新输入!', 'data':''}
    elif strUsername in login_failure_counter and login_failure_counter[strUsername]["cnt"] >= lockCntThreshold and (datetime.datetime.now() - login_failure_counter[strUsername]["last_failure_time"]).seconds <= lockTimeThreshold:
        result = {'status':3, 'msg':'登录失败超过5次，该账号已被锁定5分钟!', 'data':''}
    else:
        correct_users = users.objects.filter(username=strUsername)
        if len(correct_users) == 0:
            result = {'status':4, 'msg':'该用户不存在!', 'data':''}
        elif len(correct_users) == 1 and check_password(strPassword, correct_users[0].password) == True:
            #调用了django内置函数check_password函数检测输入的密码是否与django默认的PBKDF2算法相匹配
            if strUsername in login_failure_counter:
                #如果登录失败计数器中存在该用户名，则清除之
                login_failure_counter.pop(strUsername)
            result = {'status':0, 'msg':'ok', 'data':''}
        else:
            if strUsername not in login_failure_counter:
                #第一次登录失败，登录失败计数器中不存在该用户，则创建一个该用户的计数器
                login_failure_counter[strUsername] = {"cnt":1, "last_failure_time":datetime.datetime.now()}
            else:
                if (datetime.datetime.now() - login_failure_counter[strUsername]["last_failure_time"]).seconds <= lockTimeThreshold:
                    login_failure_counter[strUsername]["cnt"] += 1
                else:
                    #上一次登录失败时间早于5分钟前，则重新计数。以达到超过5分钟自动解锁的目的。
                    login_failure_counter[strUsername]["cnt"] = 1
                login_failure_counter[strUsername]["last_failure_time"] = datetime.datetime.now()
            result = {'status':1, 'msg':'用户名或密码错误，请重新输入！', 'data':''}
    return result

#ajax接口，登录页面调用，用来验证用户名密码
@csrf_exempt
def authenticateEntry(request):
    """接收http请求，然后把请求中的用户名密码传给loginAuthenticate去验证"""
    if request.is_ajax():
        strUsername = request.POST.get('username')
        strPassword = request.POST.get('password')
    else:
        strUsername = request.POST['username']
        strPassword = request.POST['password']
    result = loginAuthenticate(strUsername, strPassword)
    request.session['login_username'] = strUsername
    return HttpResponse(json.dumps(result), content_type='application/json')


#提交SQL给inception进行自动审核
@csrf_exempt
def simplecheck(request):
    if request.is_ajax():
        sqlContent = request.POST.get('sql_content')
        clusterName = request.POST.get('cluster_name')
    else:
        sqlContent = request.POST['sql_content']
        clusterName = request.POST['cluster_name']
     
    finalResult = {'status':0, 'msg':'ok', 'data':[]}
    #服务器端参数验证
    if sqlContent is None or clusterName is None:
        finalResult['status'] = 1
        finalResult['msg'] = '页面提交参数可能为空'
        return HttpResponse(json.dumps(finalResult), content_type='application/json')

    sqlContent = sqlContent.rstrip()
    if sqlContent[-1] != ";":
        finalResult['status'] = 1
        finalResult['msg'] = 'SQL语句结尾没有以;结尾，请重新修改并提交！'
        return HttpResponse(json.dumps(finalResult), content_type='application/json')
    #交给inception进行自动审核
    result = inceptionDao.sqlautoReview(sqlContent, clusterName)
    if result is None or len(result) == 0:
        finalResult['status'] = 1
        finalResult['msg'] = 'inception返回的结果集为空！可能是SQL语句有语法错误'
        return HttpResponse(json.dumps(finalResult), content_type='application/json')
    #要把result转成JSON存进数据库里，方便SQL单子详细信息展示
    finalResult['data'] = result
    return HttpResponse(json.dumps(finalResult), content_type='application/json')


#请求图表数据
@csrf_exempt
def getMonthCharts(request):
    result = dao.getWorkChartsByMonth()
    return HttpResponse(json.dumps(result), content_type='application/json')

@csrf_exempt
def getPersonCharts(request):
    result = dao.getWorkChartsByPerson()
    return HttpResponse(json.dumps(result), content_type='application/json')

def getSqlSHA1(workflowId):
    """调用django ORM从数据库里查出review_content，从其中获取sqlSHA1值"""
    workflowDetail = get_object_or_404(workflow, pk=workflowId)
    dictSHA1 = {}
    # 使用json.loads方法，把review_content从str转成list,
    # 格式：[[1, "CHECKED", 0, "Audit completed", "None", "use bksnap", 0, "'0_0_0'", "None", "0", ""], [2, "CHECKED", 0, "Audit completed", "None", "ALTER TABLE `ts_bb_score_3`\r\n\tCOMMENT='1'", 599668, "'0_0_1'", "192_168_100_42_3306_bksnap", "0", "*43AE56C14F84EAAD2424FCBFF85ABF83C51C8CE8"]]
    listReviewContent = json.loads(workflowDetail.review_content)
    listSplitedReviewContent = listReviewContent
    # 先屏蔽BUG，listSplitedReviewContent = inceptionDao.sqlautoReview(workflowDetail.sql_content, workflowDetail.cluster_name, isSplit="yes")
    if listReviewContent == listSplitedReviewContent:
        # 拿创建工单时自动审核的结果，与split之后的审核结果对比，如果一致，说明sqlContent中不存在DML和DDL混用的情况；反之则有2种情况：1.两次审核的受影响行数等存在差别，保险起见，用第二次的审核结果；2.存在混用，需要从split之后的审核结果中取SHA1值
        for row in listReviewContent:
            id = row[0]
            sqlSHA1 = row[10]
            if sqlSHA1 != '':
                dictSHA1[id] = sqlSHA1
    else:
        for reviewContent in listReviewContent:
            id = reviewContent[0]
            sqlContent = reviewContent[5]
            backup_dbname = reviewContent[8]
            sqlSHA1 = reviewContent[10]
            if sqlSHA1 == '':
                pass
            else:
                for splitedReviewContent in listSplitedReviewContent[id-1:]:
                    # 从第[id-1]个元素开始，顺序扫描listSplitedReviewContent，一行行比对sqlContent和backup_dbname,如果相同，说明是同一条SQL，取其SHA1值存入dictSHA1[id]，break跳出;否则，继续比对下一行。
                    if splitedReviewContent[10] == '':
                        #没有SHA1值的行，可以忽略
                        pass
                    else:
                        if sqlContent == splitedReviewContent[5] and backup_dbname == splitedReviewContent[8]:
                            dictSHA1[id] = splitedReviewContent[10]
                            # print("sqlContent:  " + sqlContent + "sqlSHA1:  " + sqlSHA1)
                            # print("sqlContent2:  " + splitedReviewContent[5] + "，  sqlSHA1_2:  " + splitedReviewContent[10])
                            break
                        else:
                            pass

    if dictSHA1 != {}:
        # 如果找到有sqlSHA1值，说明是通过pt-OSC操作的，将其放入缓存。因为工单一旦提交，就被自动审核一次，且只会审核一次，
        # 而且使用OSC执行的工单更是少数，所以不设置缓存过期时间
        sqlSHA1_cache[workflowId] = dictSHA1
    return dictSHA1

@csrf_exempt
def getOscPercent(request):
    """获取该SQL的pt-OSC执行进度和剩余时间"""
    workflowId = request.POST['workflowid']
    sqlID = request.POST['sqlID']
    if workflowId == '' or workflowId is None or sqlID == '' or sqlID is None:
        context = {"status":-1 ,'msg': 'workflowId或sqlID参数为空.', "data":""}
        return HttpResponse(json.dumps(context), content_type='application/json')

    workflowId = int(workflowId)
    sqlID = int(sqlID)
    if workflowId in sqlSHA1_cache:
        dictSHA1 = sqlSHA1_cache[workflowId]
        # cachehit = "已命中"
    else:
        dictSHA1 = getSqlSHA1(workflowId)
        # cachehit = "未命中"
    if dictSHA1 != {} and sqlID in dictSHA1:
        sqlSHA1 = dictSHA1[sqlID]
        pctResult = inceptionDao.getOscPercent(sqlSHA1)  #成功获取到SHA1值，去inception里面查询进度
    else:
        pctResult = {"status":4, "msg":"不是由pt-OSC执行的", "data":""}
    return HttpResponse(json.dumps(pctResult), content_type='application/json')

@csrf_exempt
def getWorkflowStatus(request):
    """获取某个工单的当前状态"""
    workflowId = request.POST['workflowid']
    if workflowId == '' or workflowId is None :
        context = {"status":-1 ,'msg': 'workflowId参数为空.', "data":""}
        return HttpResponse(json.dumps(context), content_type='application/json')

    workflowId = int(workflowId)
    workflowDetail = get_object_or_404(workflow, pk=workflowId)
    workflowStatus = workflowDetail.status
    result = {"status":workflowStatus, "msg":"", "data":""}
    return HttpResponse(json.dumps(result), content_type='application/json')

@csrf_exempt
def stopOscProgress(request):
    """中止该SQL的pt-OSC进程"""
    workflowId = request.POST['workflowid']
    sqlID = request.POST['sqlID']
    if workflowId == '' or workflowId is None or sqlID == '' or sqlID is None:
        context = {"status":-1 ,'msg': 'workflowId或sqlID参数为空.', "data":""}
        return HttpResponse(json.dumps(context), content_type='application/json')

    loginUser = request.session.get('login_username', False)
    workflowDetail = workflow.objects.get(id=workflowId)
    try:
        listAllReviewMen = json.loads(workflowDetail.review_man)
    except ValueError:
        listAllReviewMen = (workflowDetail.review_man, )
    #服务器端二次验证，当前工单状态必须为等待人工审核,正在执行人工审核动作的当前登录用户必须为审核人. 避免攻击或被接口测试工具强行绕过
    if workflowDetail.status != Const.workflowStatus['executing']:
        context = {"status":-1, "msg":'当前工单状态不是"执行中"，请刷新当前页面！', "data":""}
        return HttpResponse(json.dumps(context), content_type='application/json')
    if loginUser is None or loginUser not in listAllReviewMen:
        context = {"status":-1 ,'msg': '当前登录用户不是审核人，请重新登录.', "data":""}
        return HttpResponse(json.dumps(context), content_type='application/json')

    workflowId = int(workflowId)
    sqlID = int(sqlID)
    if workflowId in sqlSHA1_cache:
        dictSHA1 = sqlSHA1_cache[workflowId]
    else:
        dictSHA1 = getSqlSHA1(workflowId)
    if dictSHA1 != {} and sqlID in dictSHA1:
        sqlSHA1 = dictSHA1[sqlID]
        optResult = inceptionDao.stopOscProgress(sqlSHA1)
    else:
            optResult = {"status":4, "msg":"不是由pt-OSC执行的", "data":""}
    return HttpResponse(json.dumps(optResult), content_type='application/json')

@csrf_exempt
def templateList(request):
    result = """CREATE TABLE `默认表` (
    `id` bigint unsigned NOT NULL AUTO_INCREMENT COMMENT 'ID',
    `create_time` timestamp(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '创建时间',
    `update_time` timestamp(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '更新时间',
    `valid` tinyint(1) unsigned NOT NULL DEFAULT '1' COMMENT '是否有效',
    PRIMARY KEY (`id`),
    KEY `idx_create_time` (`create_time`),
    KEY `idx_update_time` (`update_time`)
    ) ENGINE=InnoDB AUTO_INCREMENT=1 DEFAULT CHARSET=utf8mb4 COMMENT='默认表的备注')
    };
    """
    return HttpResponse(json.dumps(result), content_type='application/json')
